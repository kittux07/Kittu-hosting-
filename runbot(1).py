# -*- coding: utf-8 -*-
"""
CAMADS STANDALONE BOT HOSTING  (single file, NO MongoDB needed)
================================================================
Ek Telegram bot jo dusre bots (.py / .js / .zip / Git repo) host karta hai.

Features
  - /hosting panel: Add Bot, My Bots, Statistics
  - Upload .py / .js / .zip  ya  public Git repo deploy
  - Start / Stop / Restart  (+ Start All / Stop All / Restart All)
  - Live logs, full log file, "No module named X" par 1-tap pip install
  - Environment variables (KEY=value) -> .env + process env
  - Replace File (syntax check, backup, auto-restart, auto-rollback)
  - Rollback (last 3 versions), Download ZIP, Clone, Rename, Delete
  - Auto-restart (crash-loop guard), crash alerts, RAM limit, daily restart
  - Smart replace: bot ki file seedha bhejo, matching bot replace offer hota hai
  - Restart ke baad bots wapas auto-start / re-attach

Storage: MongoDB nahi. Saara data (DB + bot files + backups) ek PRIVATE Telegram CHANNEL me
         documents ki tarah save hota hai aur restart/redeploy/disk-wipe ke baad wahin se
         restore hota hai. (hosted_data/ sirf local working cache hai.)

SETUP
  1) pip install pyTelegramBotAPI psutil      (missing ho to file khud install kar leti hai)
  2) Neeche BOT_TOKEN aur OWNER_ID bharo  (ya env variables do)
  3) python hosting_bot.py  ->  Telegram me /hosting -> '☁️ Storage Channel' se channel set karo:
     private channel banao, bot ko ADMIN banao (Post + Edit/Pin messages), channel ka koi message
     bot ko forward karo. (CHANNEL_ID yahan/env me bhi daal sakte ho - optional.)
"""

import copy
import difflib
import functools
import hashlib
import html
import io
import json
import logging
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _ensure(mod, pip_name):
    try:
        __import__(mod)
        return
    except ImportError:
        pass
    base = [sys.executable, "-m", "pip", "install", "-q", pip_name]
    for extra in ([], ["--break-system-packages"]):
        try:
            subprocess.check_call(base + extra)
            return
        except Exception:
            continue
    raise SystemExit(f"'{pip_name}' install nahi hua. Manually chalao: pip install {pip_name}")


_ensure("telebot", "pyTelegramBotAPI")
_ensure("psutil", "psutil")

import psutil  # noqa: E402
import telebot  # noqa: E402
from telebot import types  # noqa: E402

# ======================================================================
# CONFIG  (yahan edit karo)
# ======================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8989011617:AAFXblA7l3zEFewSVFclMQrhgUClV-pfBtM"
OWNER_ID = int(os.environ.get("OWNER_ID") or 8735285838)
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ADMIN_IDS", ""))}   # optional: extra users

CHANNEL_ID = int(os.environ.get("CHANNEL_ID") or -1003961476498)   # -100 se shuru hona chahiye
AUTOSYNC_SECONDS = 1800         # itne sec baad bots ki files (runtime data) dobara channel me sync hoti hain
SYNC_MIN_GAP = 8                # DB upload ke beech minimum gap (sec)
TG_MAX_FILE = 19 * 1024 * 1024  # Telegram bot download limit 20 MB -> isse bada zip sync nahi hoga

DATA_DIR = Path(os.environ.get("HOSTING_DATA_DIR", "hosted_data")).expanduser().resolve()

OWNER_SLOTS = 100                # owner ke max hosted bots
ADMIN_SLOTS = 5                  # ADMIN_IDS wale users ke max hosted bots
MAX_UPLOAD_BYTES = 20 * 1024 * 1024        # Telegram bot API download limit = 20 MB
MAX_ARCHIVE_FILES = 1000
MAX_EXTRACTED_BYTES = 150 * 1024 * 1024
MAX_LOG_FILE_BYTES = 5 * 1024 * 1024
MAX_ENV_KEY_LEN = 40
PIP_TIMEOUT = 300
NPM_TIMEOUT = 300
BACKUP_KEEP = 3                  # rollback versions per bot
AUTO_ROLLBACK_SECONDS = 60       # replace ke baad itni der me crash -> purani file wapas
ALERTS_ENABLED = True
ALERT_COOLDOWN = 30
OWNER_ERROR_ALERTS = True        # True = koi bhi error/problem aaye to owner ko Telegram par REASON ke saath bata do
OWNER_ALERT_COOLDOWN = 300       # same error dobara itni der (sec) tak nahi bhejna (spam rokne ke liye)
OWNER_ALERT_MAX_PER_MIN = 6      # ek minute me max itne alerts
AUTO_INSTALL_BASE_REQ = True     # True = start par runbot.py ke saath wali requirements.txt ke packages server par install karo
BASE_REQ_FILE = Path(__file__).resolve().with_name("requirements.txt")
BASE_REQ_TIMEOUT = 900           # itne sec se zyada install me na lage
AUTO_START_ON_BOOT = True
FLOW_TTL = 600                   # seconds jab tak bot tumhara next text/file wait kare
SCHEDULE_TZ_OFFSET_MIN = 330     # daily-restart clock: IST = UTC+5:30
SCHEDULE_TZ_LABEL = "IST"
RAM_LIMIT_PRESETS = [0, 150, 300, 500, 1000]   # MB, 0 = off

VERSION = "2.2-channel"

BOTS_DIR = DATA_DIR / "bots"
BACKUPS_DIR = DATA_DIR / "backups"
DB_FILE = DATA_DIR / "hosting_db.json"
CONFIG_FILE = DATA_DIR / "config.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hosting")

# ======================================================================
# Small helpers
# ======================================================================
def esc(s):
    return html.escape(str(s if s is not None else ""))


def now():
    return int(time.time())


def tz():
    return timezone(timedelta(minutes=SCHEDULE_TZ_OFFSET_MIN))


def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=tz()).strftime("%d %b %H:%M")
    except Exception:
        return str(ts)


def fmt_bytes(n):
    n = float(max(0, n or 0))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024


def fmt_uptime(seconds):
    seconds = max(0, int(seconds or 0))
    d, seconds = divmod(seconds, 86400)
    h, seconds = divmod(seconds, 3600)
    m, s = divmod(seconds, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def fit_pre(text, limit=3000):
    """Escape text for <pre>, trimming from the start until it fits."""
    text = str(text or "")
    while text and len(esc(text)) > limit:
        text = text[max(1, len(text) // 8):]
    return esc(text)


def safe_name(name, fallback="bot"):
    name = Path(str(name or fallback)).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-") or fallback
    return name[:80]


def safe_rel(rel):
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts) or len("/".join(parts)) > 240:
        raise ValueError("Unsafe archive path")
    return "/".join(parts)


# ======================================================================
# Storage (replaces MongoDB): one JSON file, atomic writes
# ======================================================================
class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.bots = {}
        self.saved_at = 0
        self.dirty = threading.Event()      # DB channel me upload hona baaki hai
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.bots = data.get("bots", {})
                self.saved_at = int(data.get("saved_at", 0))
        except Exception:
            log.exception("hosting_db.json unreadable - starting empty (old file kept as .broken)")
            try:
                shutil.copy(self.path, str(self.path) + ".broken")
            except Exception:
                pass
            self.bots = {}

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"bots": self.bots, "saved_at": self.saved_at}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def _flush(self):
        self.saved_at = int(time.time())
        self._write()
        self.dirty.set()

    def dump_bytes(self):
        with self.lock:
            return json.dumps({"bots": self.bots, "saved_at": self.saved_at, "v": 2},
                              ensure_ascii=False, indent=1).encode("utf-8")

    def replace(self, bots, saved_at):
        """Channel se restore: local DB ko remote se badlo (dirty nahi hota)."""
        with self.lock:
            self.bots = bots
            self.saved_at = int(saved_at)
            self._write()

    def get(self, bid):
        with self.lock:
            d = self.bots.get(str(bid))
            return copy.deepcopy(d) if d else None

    def all(self):
        with self.lock:
            return [copy.deepcopy(d) for d in self.bots.values()]

    def by_owner(self, uid):
        docs = [d for d in self.all() if int(d.get("owner", 0)) == int(uid)]
        docs.sort(key=lambda d: str(d.get("name", "")).lower())
        return docs

    def save(self, doc):
        with self.lock:
            self.bots[str(doc["_id"])] = copy.deepcopy(doc)
            self._flush()

    def update(self, bid, **fields):
        with self.lock:
            d = self.bots.get(str(bid))
            if d is None:
                return None
            d.update(copy.deepcopy(fields))
            self._flush()
            return copy.deepcopy(d)

    def mutate(self, bid, fn):
        with self.lock:
            d = self.bots.get(str(bid))
            if d is None:
                return None
            fn(d)
            self._flush()
            return copy.deepcopy(d)

    def delete(self, bid):
        with self.lock:
            self.bots.pop(str(bid), None)
            self._flush()


store = None  # created in main()/init()


def bot_dir(bid):
    return BOTS_DIR / str(bid)


def log_path(bid):
    return bot_dir(bid) / ".host.log"


def pid_path(bid):
    return bot_dir(bid) / ".host.pid"


# ======================================================================
# Project files: entry detection, zip, swap
# ======================================================================
SKIP_DIRS = {".git", ".cache", ".python_deps", "node_modules", "__pycache__", ".venv", "venv"}
RUNTIME_IGNORE = (".host.pid", "*.host.log", ".git", "__pycache__", "node_modules", ".python_deps")

STRONG_HINT = re.compile(
    r"run_polling\s*\(|infinity_polling\s*\(|\.polling\s*\(|start_polling\s*\(|"
    r"run_until_disconnected\s*\(|\.idle\s*\(|\.run_forever\s*\(|\b(?:app|bot|client|application)\.run\s*\("
)
MAIN_HINT = re.compile(r"""__name__\s*==\s*["']__main__["']""")


def find_sources(root, suffix):
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            if f.endswith(suffix):
                out.append(str((Path(dp) / f).relative_to(root)).replace("\\", "/"))
    out.sort()
    return out


def guess_py_entry(root):
    root = Path(root)
    files = find_sources(root, ".py")
    if not files:
        return None
    scored = []
    for rel in files[:200]:
        score = 0
        try:
            text = (root / rel).read_bytes()[:300_000].decode("utf-8", "ignore")
            score = 2 if STRONG_HINT.search(text) else (1 if MAIN_HINT.search(text) else 0)
        except Exception:
            pass
        scored.append((-score, rel.count("/"), rel))
    scored.sort()
    return scored[0][2]


def guess_js_entry(root):
    root = Path(root)
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            main = json.loads(pkg.read_text(encoding="utf-8")).get("main")
            if isinstance(main, str) and main.endswith(".js"):
                norm = os.path.normpath(main).replace("\\", "/")
                if not norm.startswith("..") and not os.path.isabs(norm) and (root / norm).is_file():
                    return norm
        except Exception:
            pass
    files = find_sources(root, ".js")
    files.sort(key=lambda r: (r.count("/"), r))
    return files[0] if files else None


def entry_for_dir(root):
    root = Path(root)
    for x in ("bot.py", "main.py", "app.py", "run.py", "index.py"):
        if (root / x).is_file():
            return "py", x
    for x in ("index.js", "main.js", "bot.js", "app.js"):
        if (root / x).is_file():
            return "js", x
    if (root / "package.json").is_file():
        j = guess_js_entry(root)
        if j:
            return "js", j
    p = guess_py_entry(root)
    if p:
        return "py", p
    j = guess_js_entry(root)
    if j:
        return "js", j
    return None, None


def flatten_single_dir(project):
    project = Path(project)
    for _ in range(3):
        shutil.rmtree(project / "__MACOSX", ignore_errors=True)
        items = [p for p in project.iterdir() if p.name != ".DS_Store"]
        if len(items) != 1 or not items[0].is_dir() or items[0].is_symlink():
            return
        tmp = project / ".__flat_tmp"
        items[0].rename(tmp)
        for child in list(tmp.iterdir()):
            child.rename(project / child.name)
        tmp.rmdir()


def extract_zip(blob, target):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    total = count = 0
    with zipfile.ZipFile(io.BytesIO(blob), "r") as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > MAX_ARCHIVE_FILES:
            raise ValueError("ZIP me bahut zyada files hain")
        for info in infos:
            rel = safe_rel(info.filename)
            total += int(info.file_size)
            count += 1
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError("ZIP extract hone ke baad bahut bada hai")
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info, "r") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    return count, total


def tree_stats(root):
    total = count = 0
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            if f == ".host.pid" or f.endswith(".host.log"):
                continue
            p = Path(dp) / f
            if p.is_symlink() or not p.is_file():
                continue
            total += p.stat().st_size
            count += 1
            if count > MAX_ARCHIVE_FILES or total > MAX_EXTRACTED_BYTES:
                raise ValueError("Project bahut bada hai")
    return count, total


def zip_dir(root):
    root = Path(root)
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, dn, fn in os.walk(root):
            dn[:] = sorted(d for d in dn if d not in SKIP_DIRS)
            for f in sorted(fn):
                if f == ".host.pid" or f.endswith(".host.log"):
                    continue
                p = Path(dp) / f
                if p.is_symlink() or not p.is_file():
                    continue
                z.write(p, str(p.relative_to(root)).replace("\\", "/"))
    return raw.getvalue()


def swap_project(root, staged):
    """Replace content of root with staged (keeps log, deps, node_modules)."""
    keep = {".host.log", ".python_deps", "node_modules"}
    root = Path(root)
    if root.exists():
        for child in root.iterdir():
            if child.name in keep:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(staged, root, dirs_exist_ok=True)


def diff_summary(old_root, new_root):
    def files(root):
        out = {}
        root = Path(root)
        if not root.exists():
            return out
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if d not in SKIP_DIRS]
            for f in fn:
                if f == ".host.pid" or f.endswith(".host.log"):
                    continue
                p = Path(dp) / f
                if p.is_file() and not p.is_symlink():
                    out[str(p.relative_to(root)).replace("\\", "/")] = p
        return out

    a, b = files(old_root), files(new_root)
    added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
    changed, plus, minus = [], 0, 0
    for rel in sorted(set(a) & set(b)):
        try:
            da, db = a[rel].read_bytes(), b[rel].read_bytes()
        except Exception:
            continue
        if da == db:
            continue
        p_ = m_ = 0
        if len(da) < 400_000 and len(db) < 400_000:
            for line in difflib.unified_diff(da.decode("utf-8", "ignore").splitlines(),
                                             db.decode("utf-8", "ignore").splitlines(), lineterm="", n=0):
                if line.startswith("+") and not line.startswith("+++"):
                    p_ += 1
                elif line.startswith("-") and not line.startswith("---"):
                    m_ += 1
        changed.append((rel, p_, m_))
        plus += p_
        minus += m_
    for rel in added:
        try:
            plus += len(b[rel].read_bytes().splitlines())
        except Exception:
            pass
    if not (added or removed or changed):
        return "Koi change nahi (file same hai)."
    lines = [f"✏️ Changed: {len(changed)} • ➕ New: {len(added)} • ➖ Removed: {len(removed)}",
             f"📊 Lines: +{plus} / -{minus}"]
    for rel, p_, m_ in changed[:5]:
        lines.append(f"• {rel}: +{p_} / -{m_}")
    return "\n".join(lines)


# ======================================================================
# Environment variables (.env managed block + safe process env)
# ======================================================================
ENV_BEGIN = "# --- CAMADS HOST ENV BEGIN (managed by host, do not edit) ---"
ENV_END = "# --- CAMADS HOST ENV END ---"
ENV_BLOCK_RE = re.compile(re.escape(ENV_BEGIN) + r".*?" + re.escape(ENV_END) + r"\n?", re.S)

# Hosted bots host-bot ke secrets (BOT_TOKEN, ...) inherit NAHI karte.
HOST_ENV_ALLOW = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "TMPDIR", "TEMP", "TMP", "LANG", "LANGUAGE",
    "PYTHONUTF8", "PYTHONIOENCODING", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "NODE_PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR",
}


def write_dotenv(root, env):
    path = Path(root) / ".env"
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except Exception:
        existing = ""
    base = ENV_BLOCK_RE.sub("", existing)
    had_block = base != existing
    if not env and not had_block:
        return
    try:
        if not env:
            if base.strip():
                path.write_text(base, encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
            return
        lines = []
        for k, v in env.items():
            val = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            lines.append(f'{k}="{val}"')
        block = ENV_BEGIN + "\n" + "\n".join(lines) + "\n" + ENV_END + "\n"
        head = base.rstrip("\n")
        new = (head + "\n" if head else "") + block
        if new != existing:
            path.write_text(new, encoding="utf-8")
    except Exception:
        pass


def safe_env(root, extra):
    env = {k: v for k, v in os.environ.items() if k in HOST_ENV_ALLOW or k.startswith("LC_")}
    protected = {"PATH", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH"}
    for k, v in (extra or {}).items():
        k = str(k)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and k not in protected:
            env[k] = str(v)
    env["CAMADS_BOT_DIR"] = str(root)
    return env


# ======================================================================
# Logs
# ======================================================================
def append_log(bid, text):
    if not text:
        return
    try:
        with open(log_path(bid), "ab") as f:
            f.write((str(text).rstrip("\n") + "\n").encode("utf-8", "replace"))
    except Exception:
        pass


def read_log_tail(bid, max_lines=100, max_bytes=32768):
    p = log_path(bid)
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        lines = data.decode("utf-8", "replace").splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def rotate_log(path, limit=MAX_LOG_FILE_BYTES, keep=256 * 1024):
    try:
        if path.exists() and path.stat().st_size > limit:
            path.write_bytes(path.read_bytes()[-keep:])
    except Exception:
        pass


def trim_log(bid, limit=MAX_LOG_FILE_BYTES):
    p = log_path(bid)
    try:
        if p.stat().st_size > limit:
            os.truncate(p, 0)
            append_log(bid, "[log truncated by host]")
    except Exception:
        pass


# ======================================================================
# Process management
# ======================================================================
PROCS = {}                       # bid -> {popen, pid, started, manual_stop, entry, kind, rollback_to}
BLOCKS = defaultdict(threading.RLock)   # per-bot lock
RESTARTS = {}
LAST_ALERT = {}
PIP_SUGGEST = {}
PS_CACHE = {}


def pid_alive(pid):
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def info_alive(info):
    if not info:
        return False
    po = info.get("popen")
    if po is not None:
        return po.poll() is None
    return pid_alive(info["pid"])


def read_pidfile(bid):
    try:
        return int(pid_path(bid).read_text().strip())
    except Exception:
        return None


def write_pidfile(bid, pid):
    try:
        pid_path(bid).write_text(str(pid))
    except Exception:
        pass


def cwd_matches(p, root):
    try:
        return os.path.realpath(p.cwd()) == os.path.realpath(str(root))
    except Exception:
        return False


def dir_procs(bid):
    """All live processes whose cwd is this bot's folder (never the host itself)."""
    root = bot_dir(bid)
    me = os.getpid()
    out = []
    for p in psutil.process_iter(["pid"]):
        try:
            if p.pid == me or p.status() == psutil.STATUS_ZOMBIE:
                continue
            if cwd_matches(p, root):
                out.append(p)
        except Exception:
            continue
    return out


def terminate_procs(procs, grace=4.0):
    procs = list(procs)
    if not procs:
        return 0
    everyone = []
    for p in procs:
        try:
            everyone.append(p)
            everyone.extend(p.children(recursive=True))
        except Exception:
            pass
    seen, uniq = set(), []
    for p in everyone:
        if p.pid not in seen and p.pid != os.getpid():
            seen.add(p.pid)
            uniq.append(p)
    for p in uniq:
        try:
            p.terminate()
        except Exception:
            pass
    try:
        _gone, alive = psutil.wait_procs(uniq, timeout=grace)
    except Exception:
        alive = []
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass
    return len(uniq)


def try_adopt(bid, entry=None, kind=None):
    """Re-attach to a hosted process that survived a host-bot restart."""
    if bid in PROCS and info_alive(PROCS[bid]):
        return True
    pid = read_pidfile(bid)
    if not pid or not pid_alive(pid):
        return False
    try:
        p = psutil.Process(pid)
        if not cwd_matches(p, bot_dir(bid)):
            return False
        PROCS[bid] = {"popen": None, "pid": pid, "started": int(p.create_time()), "manual_stop": False,
                      "entry": entry, "kind": kind}
        return True
    except Exception:
        return False


def rss_of(pid):
    try:
        p = psutil.Process(pid)
        total = p.memory_info().rss
        for c in p.children(recursive=True):
            try:
                total += c.memory_info().rss
            except Exception:
                pass
        return total
    except Exception:
        return 0


def status_of(b):
    bid = b["_id"]
    info = PROCS.get(bid)
    if info and info_alive(info):
        cpu = 0.0
        try:
            p = PS_CACHE.get(info["pid"])
            if p is None or not p.is_running():
                if len(PS_CACHE) > 64:
                    PS_CACHE.clear()
                p = psutil.Process(info["pid"])
                p.cpu_percent(None)
                PS_CACHE[info["pid"]] = p
            cpu = p.cpu_percent(None)
        except Exception:
            pass
        return {"running": True, "pid": info["pid"], "cpu": cpu, "mem": rss_of(info["pid"]),
                "uptime": now() - int(info.get("started", now())), "entry": b.get("entry") or "—"}
    return {"running": False, "pid": None, "cpu": 0.0, "mem": 0, "uptime": 0, "entry": b.get("entry") or "—"}


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def install_py_deps(root):
    req = root / "requirements.txt"
    dep_dir = root / ".python_deps"
    dep_dir.mkdir(exist_ok=True)
    marker = dep_dir / ".req_hash"
    h = file_hash(req)
    try:
        if marker.read_text().strip() == h:
            return None
    except Exception:
        pass
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
           "--upgrade", "--target", str(dep_dir), "-r", str(req)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT, cwd=str(root))
    except subprocess.TimeoutExpired:
        return f"requirements.txt install {PIP_TIMEOUT}s me timeout ho gaya"
    except Exception as e:
        return f"pip install failed: {e}"
    if r.returncode != 0:
        return "requirements.txt install failed: " + (r.stderr or r.stdout)[-700:]
    try:
        marker.write_text(h)
    except Exception:
        pass
    return None


def install_node_deps(root):
    pkg = root / "package.json"
    nm = root / "node_modules"
    marker = nm / ".pkg_hash"
    h = file_hash(pkg)
    try:
        if marker.read_text().strip() == h:
            return None
    except Exception:
        pass
    try:
        r = subprocess.run(["npm", "install", "--no-audit", "--no-fund"], cwd=str(root),
                           capture_output=True, text=True, timeout=NPM_TIMEOUT)
    except FileNotFoundError:
        return "Node.js/npm is server par installed nahi hai"
    except subprocess.TimeoutExpired:
        return f"npm install {NPM_TIMEOUT}s me timeout ho gaya"
    except Exception as e:
        return f"npm install failed: {e}"
    if r.returncode != 0:
        return "npm install failed: " + (r.stderr or r.stdout)[-700:]
    try:
        nm.mkdir(exist_ok=True)
        marker.write_text(h)
    except Exception:
        pass
    return None


def start_bot(bid):
    with BLOCKS[bid]:
        b = store.get(bid)
        if not b:
            return {"ok": False, "error": "Bot not found"}
        info = PROCS.get(bid)
        if info and info_alive(info):
            return {"ok": True, "already": True, "pid": info["pid"]}
        root = bot_dir(bid)
        root.mkdir(parents=True, exist_ok=True)
        kind, entry = entry_for_dir(root)
        if not kind:
            return {"ok": False, "error": "Entry file nahi mili (bot.py/main.py/app.py/index.js ...). Files missing ho sakti hain."}

        if try_adopt(bid, entry, kind):
            store.update(bid, status="running", entry=entry, kind=kind, last_error="")
            return {"ok": True, "adopted": True, "pid": PROCS[bid]["pid"]}

        stale = terminate_procs(dir_procs(bid))     # kabhi 2 instances nahi (same token = double replies)
        if stale:
            append_log(bid, f"[host: killed {stale} stale instance(s) before start]")

        if kind == "py" and (root / "requirements.txt").exists():
            err = install_py_deps(root)
            if err:
                return {"ok": False, "error": err}
        elif kind == "js" and (root / "package.json").exists():
            err = install_node_deps(root)
            if err:
                return {"ok": False, "error": err}

        write_dotenv(root, b.get("env") or {})
        env = safe_env(root, b.get("env") or {})
        if kind == "py":
            dep = str(root / ".python_deps")
            old = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = dep + (os.pathsep + old if old else "")
            cmd = [sys.executable, "-u", entry]
        else:
            cmd = ["node", entry]

        lf = log_path(bid)
        rotate_log(lf)
        append_log(bid, f"=== CAMADS HOST START {now()} ===")
        try:
            logf = open(lf, "ab", buffering=0)
        except Exception as e:
            return {"ok": False, "error": f"log file open nahi hui: {e}"}
        kw = {"start_new_session": True} if os.name == "posix" else {}
        try:
            proc = subprocess.Popen(cmd, cwd=str(root), env=env, stdout=logf, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, **kw)
        except Exception as e:
            return {"ok": False, "error": f"spawn failed: {e}"}
        finally:
            logf.close()

        PROCS[bid] = {"popen": proc, "pid": proc.pid, "started": now(), "manual_stop": False,
                      "entry": entry, "kind": kind}
        write_pidfile(bid, proc.pid)
        store.update(bid, status="running", entry=entry, kind=kind, last_started=now(), last_error="")
        return {"ok": True, "pid": proc.pid, "kind": kind, "entry": entry}


def stop_bot(bid, manual=True):
    with BLOCKS[bid]:
        info = PROCS.get(bid)
        if info:
            info["manual_stop"] = manual
        targets = []
        pid = info["pid"] if info else read_pidfile(bid)
        if pid and pid_alive(pid):
            try:
                p = psutil.Process(pid)
                if cwd_matches(p, bot_dir(bid)) or info:
                    targets.append(p)
            except Exception:
                pass
        targets.extend(dir_procs(bid))
        terminate_procs(targets)
        if info and info.get("popen") is not None:
            try:
                info["popen"].wait(timeout=2)
            except Exception:
                pass
        PROCS.pop(bid, None)
        try:
            pid_path(bid).unlink(missing_ok=True)
        except Exception:
            pass
        if manual and store.get(bid):
            store.update(bid, status="stopped")
        return True


def restart_bot(bid):
    with BLOCKS[bid]:
        stop_bot(bid, True)
        time.sleep(0.7)
        return start_bot(bid)


def restart_allowed(bid, max_restarts=5, window=300):
    t = time.time()
    hist = [x for x in RESTARTS.get(bid, []) if t - x < window]
    if len(hist) >= max_restarts:
        RESTARTS[bid] = hist
        return False
    hist.append(t)
    RESTARTS[bid] = hist
    return True


# ======================================================================
# Backups / rollback (files on disk, index in the bot doc)
# ======================================================================
def get_backups(bid):
    b = store.get(bid) or {}
    return sorted(b.get("backups", []), key=lambda x: x.get("ms", 0), reverse=True)


def make_backup(bid, label):
    root = bot_dir(bid)
    if not root.exists():
        return None
    try:
        blob = zip_dir(root)
        ms = int(time.time() * 1000)
        d = BACKUPS_DIR / bid
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{ms}.zip").write_bytes(blob)
        items = get_backups(bid)
        items.insert(0, {"ms": ms, "ts": ms // 1000, "label": str(label)[:80], "size": len(blob)})
        for old in items[BACKUP_KEEP:]:
            (d / f"{old['ms']}.zip").unlink(missing_ok=True)
            sync.defer_delete(old.get("tg_msg"))
        store.update(bid, backups=items[:BACKUP_KEEP])
        sync.mark_backup(bid, ms)
        return ms
    except Exception as e:
        log.warning("backup skipped for %s: %s", bid, e)
        return None


def restore_backup(bid, ms, make_new_backup=True):
    f = BACKUPS_DIR / bid / f"{ms}.zip"
    blob = f.read_bytes() if f.exists() else None
    if blob is None:        # local copy wipe ho gayi -> channel se lao
        ent = next((x for x in get_backups(bid) if str(x.get("ms")) == str(ms)), None)
        if ent and ent.get("tg_file"):
            try:
                blob = sync.download(ent["tg_file"])
            except Exception:
                blob = None
    if blob is None:
        return {"ok": False, "error": "Backup nahi mila."}
    tmp = Path(tempfile.mkdtemp(prefix="camads_rb_"))
    try:
        staged = tmp / "staged"
        extract_zip(blob, staged)
        with BLOCKS[bid]:
            was_running = status_of(store.get(bid))["running"]
            stop_bot(bid, True)
            root = bot_dir(bid)
            if make_new_backup:
                make_backup(bid, "before rollback")
            swap_project(root, staged)
            kind, entry = entry_for_dir(root)
            b = store.get(bid)
            write_dotenv(root, b.get("env") or {})
            fields = {"last_error": ""}
            if kind:
                fields.update(kind=kind, entry=entry)
            store.update(bid, **fields)
            append_log(bid, f"=== ROLLBACK to version of {fmt_ts(ms // 1000)} ===")
            RESTARTS.pop(bid, None)
        sync.mark_bot(bid)
        return {"ok": True, "was_running": was_running}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ======================================================================
# Missing-module detection -> pip buttons
# ======================================================================
PIP_NAME_MAP = {
    "PIL": "Pillow", "cv2": "opencv-python-headless", "bs4": "beautifulsoup4", "yaml": "PyYAML",
    "telegram": "python-telegram-bot", "telebot": "pyTelegramBotAPI", "dotenv": "python-dotenv",
    "sklearn": "scikit-learn", "Crypto": "pycryptodome", "dateutil": "python-dateutil", "jwt": "PyJWT",
    "bson": "pymongo", "gridfs": "pymongo", "fitz": "PyMuPDF", "docx": "python-docx",
    "OpenSSL": "pyOpenSSL", "attr": "attrs", "googleapiclient": "google-api-python-client",
}
MISSING_RE = re.compile(r"No module named ['\"]([A-Za-z0-9_\.]+)['\"]")
PIP_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def missing_modules(text, root):
    stdlib = getattr(sys, "stdlib_module_names", set())
    seen, out = set(), []
    for m in MISSING_RE.finditer(text or ""):
        top = m.group(1).split(".")[0]
        if top in seen or top in stdlib:
            continue
        seen.add(top)
        if (Path(root) / f"{top}.py").exists() or (Path(root) / top).is_dir():
            continue
        pip = PIP_NAME_MAP.get(top, top)
        if PIP_OK.match(pip):
            out.append((top, pip))
    return out[-3:]


def pip_install_one(root, pipname):
    dep_dir = Path(root) / ".python_deps"
    dep_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
           "--upgrade", "--target", str(dep_dir), pipname]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT, cwd=str(root))
    except subprocess.TimeoutExpired:
        return f"pip timeout ({PIP_TIMEOUT}s)"
    except Exception as e:
        return f"pip failed: {e}"
    if r.returncode != 0:
        return ((r.stderr or r.stdout) or "pip failed")[-700:]
    req = Path(root) / "requirements.txt"      # redeploy par bhi install ho
    try:
        cur = req.read_text(encoding="utf-8") if req.exists() else ""
        names = {re.split(r"[<>=!~\[; ]", ln.strip(), maxsplit=1)[0].lower().replace("_", "-")
                 for ln in cur.splitlines() if ln.strip() and not ln.strip().startswith("#")}
        if pipname.lower().replace("_", "-") not in names:
            req.write_text(cur.rstrip("\n") + ("\n" if cur.strip() else "") + pipname + "\n", encoding="utf-8")
    except Exception:
        pass
    return None


# ======================================================================
# Telegram CHANNEL storage (MongoDB replacement)
#   - hosting_db.json  -> channel me document, PINNED (boot par yahin se milta hai)
#   - har bot ka zip   -> channel me document (file_id DB me saved)
#   - rollback backups -> channel me document
# ======================================================================
sync = None   # TgSync(), init() me bana


class TgSync:
    def __init__(self):
        self.enabled = bool(CHANNEL_ID)
        self.lock = threading.RLock()
        self.pending = set()
        self.backups_pending = []
        self.retry = {}
        self.last_full = time.time()
        self.last_db_push = 0.0
        self.db_msg_id = None
        self.last_ok = 0
        self.last_error = ""
        self.warned = set()
        self._thread = None
        self.garbage = []          # purane channel messages: naya DB pin hone ke BAAD delete honge
        self.wake = threading.Event()

    # ---- queueing
    def mark_bot(self, bid):
        if self.enabled:
            with self.lock:
                self.pending.add(str(bid))
            self.wake.set()

    def mark_backup(self, bid, ms):
        if self.enabled:
            with self.lock:
                self.backups_pending.append((str(bid), int(ms)))
            self.wake.set()

    # ---- telegram io
    def _send(self, data, filename, caption):
        bio = io.BytesIO(data)
        bio.name = filename
        m = bot.send_document(CHANNEL_ID, bio, caption=caption[:900])
        return m.message_id, m.document.file_id

    def download(self, file_id):
        info = bot.get_file(file_id)
        return bot.download_file(info.file_path)

    def delete_msg(self, mid):
        if not (self.enabled and mid):
            return
        try:
            bot.delete_message(CHANNEL_ID, int(mid))
        except Exception:
            pass

    def defer_delete(self, mid):
        """Channel DB abhi purani file_id ko point karta hai - naya DB upload hone ke baad hi delete karo."""
        if self.enabled and mid:
            with self.lock:
                self.garbage.append(int(mid))

    def _purge(self):
        with self.lock:
            items, self.garbage = self.garbage, []
        for mid in items:
            self.delete_msg(mid)

    def delete_bot_msgs(self, b):
        if not self.enabled or not b:
            return
        self.defer_delete(b.get("tg_msg"))
        for x in b.get("backups", []):
            self.defer_delete(x.get("tg_msg"))

    def _too_big(self, key, what, size):
        if key in self.warned:
            return
        self.warned.add(key)
        notify(OWNER_ID, f"⚠️ <b>Channel sync skip:</b> {esc(what)} zip {fmt_bytes(size)} hai "
                         f"(limit {fmt_bytes(TG_MAX_FILE)}). Is bot ka data restart par restore NAHI hoga. "
                         "Project chhota karo (bade data/media files hatao).")

    # ---- pushes
    def push_bot(self, bid):
        b = store.get(bid)
        root = bot_dir(bid)
        if not b or not root.exists():
            return
        blob = zip_dir(root)
        h = hashlib.sha256(blob).hexdigest()
        if b.get("tg_hash") == h and b.get("tg_file"):
            return
        if len(blob) > TG_MAX_FILE:
            self._too_big(f"{bid}:{h[:8]}", b.get("name", bid), len(blob))
            return
        mid, fid = self._send(blob, f"{safe_name(b.get('name'))}_{bid}.zip",
                              f"#bot {bid} {b.get('name')} • {fmt_ts(now())}")
        old = b.get("tg_msg")
        store.update(bid, tg_msg=mid, tg_file=fid, tg_hash=h, tg_ts=now())
        if old:
            self.defer_delete(old)
        self.last_ok = now()

    def push_backup(self, bid, ms):
        f = BACKUPS_DIR / bid / f"{ms}.zip"
        if not f.exists():
            return
        blob = f.read_bytes()
        if len(blob) > TG_MAX_FILE:
            self._too_big(f"{bid}:bk{ms}", "backup", len(blob))
            return
        mid, fid = self._send(blob, f"backup_{bid}_{ms}.zip", f"#backup {bid} {fmt_ts(ms // 1000)}")

        def _set(d):
            hit = False
            for x in d.get("backups", []):
                if x.get("ms") == ms:
                    x["tg_msg"], x["tg_file"] = mid, fid
                    hit = True
            return hit

        found = []
        store.mutate(bid, lambda d: found.append(_set(d)))
        if not (found and found[0]):       # backup is beech me trim ho gaya
            self.delete_msg(mid)

    def push_db(self):
        store.dirty.clear()
        data = store.dump_bytes()
        mid, _fid = self._send(data, "hosting_db.json", f"#db {fmt_ts(now())}")
        try:
            bot.pin_chat_message(CHANNEL_ID, mid, disable_notification=True)
        except Exception as e:
            self.last_error = f"pin failed (bot ko Pin/Edit messages permission do): {e}"
            log.warning(self.last_error)
        old, self.db_msg_id = self.db_msg_id, mid
        if old and old != mid:
            self.delete_msg(old)
        self._purge()
        self.last_db_push = time.time()
        self.last_ok = now()

    # ---- loop
    def run_once(self):
        with self.lock:
            bots = set(self.pending)
            self.pending.clear()
            backs = list(self.backups_pending)
            self.backups_pending.clear()
        t = time.time()
        for bid, when in list(self.retry.items()):
            if when <= t:
                bots.add(bid)
                self.retry.pop(bid, None)
        if t - self.last_full >= AUTOSYNC_SECONDS:
            self.last_full = t
            bots |= {b["_id"] for b in store.all()}
        for bid in bots:
            try:
                self.push_bot(bid)
            except Exception as e:
                self.last_error = f"bot {bid}: {e}"
                self.retry[bid] = time.time() + 60
                log.warning("sync push_bot %s failed: %s", bid, e)
        for bid, ms in backs:
            try:
                self.push_backup(bid, ms)
            except Exception as e:
                self.last_error = f"backup {bid}: {e}"
                log.warning("sync push_backup failed: %s", e)
        if store.dirty.is_set() and time.time() - self.last_db_push >= SYNC_MIN_GAP:
            try:
                self.push_db()
                if not self.last_error.startswith("pin failed"):
                    self.last_error = ""
            except Exception as e:
                store.dirty.set()
                self.last_error = f"db: {e}"
                log.warning("sync push_db failed: %s", e)

    def loop(self):
        while True:
            self.wake.wait(SYNC_MIN_GAP)
            self.wake.clear()
            try:
                self.run_once()
            except Exception:
                log.exception("sync loop failed")

    def flush_all(self):
        """Sab kuch abhi channel me bhejo (Sync Now / shutdown)."""
        if not self.enabled:
            return
        for b in store.all():
            try:
                self.push_bot(b["_id"])
            except Exception as e:
                self.last_error = f"bot {b['_id']}: {e}"
        with self.lock:
            backs = list(self.backups_pending)
            self.backups_pending.clear()
        for bid, ms in backs:
            try:
                self.push_backup(bid, ms)
            except Exception as e:
                self.last_error = f"backup {bid}: {e}"
        try:
            self.push_db()
        except Exception as e:
            self.last_error = f"db: {e}"

    # ---- startup
    def ensure_thread(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self.loop, daemon=True, name="tg-sync")
            self._thread.start()

    def check(self, cid=None):
        """None agar sab theek, warna error text."""
        cid = cid or CHANNEL_ID
        try:
            me = bot.get_me()
            cm = bot.get_chat_member(cid, me.id)
        except Exception as e:
            return (f"Channel {cid} access nahi hua: {e}\n"
                    "Check: CHANNEL_ID sahi hai (-100... se shuru) aur bot channel ka ADMIN hai.")
        if getattr(cm, "status", "") not in ("administrator", "creator"):
            return "Bot channel me ADMIN nahi hai. Admin banao (Post messages + Edit/Pin messages)."
        if getattr(cm, "can_post_messages", True) is False:
            return "Bot ko channel me 'Post messages' permission do."
        return None

    def restore(self):
        """Boot par: channel se DB + missing project files wapas lao."""
        n_db = n_files = 0
        chat = bot.get_chat(CHANNEL_ID)
        pm = getattr(chat, "pinned_message", None)
        if pm is not None and getattr(pm, "document", None) is not None:
            self.db_msg_id = pm.message_id
            try:
                remote = json.loads(self.download(pm.document.file_id).decode("utf-8"))
                if int(remote.get("saved_at", 0)) > store.saved_at:
                    store.replace(remote.get("bots", {}), remote.get("saved_at", 0))
                    n_db = len(store.bots)
                    log.warning("channel se DB restore hua (%d bots)", n_db)
            except Exception:
                log.exception("channel DB restore failed")
        for b in store.all():
            bid = b["_id"]
            root = bot_dir(bid)
            have = root.exists() and entry_for_dir(root)[0]
            if have or not b.get("tg_file"):
                continue
            try:
                extract_zip(self.download(b["tg_file"]), root)
                write_dotenv(root, b.get("env") or {})
                append_log(bid, "[host: files channel se restore hui]")
                n_files += 1
            except Exception:
                log.exception("restore of %s failed", bid)
                store.update(bid, last_error="Channel se files restore nahi hui")
        store.dirty.clear()
        return n_db, n_files

    def status_text(self):
        if not self.enabled:
            return "☁️ Channel sync: <b>OFF</b> - data sirf local disk par hai. /hosting → ☁️ Storage Channel se set karo."
        t = "☁️ Channel sync: <b>ON</b>"
        if self.last_ok:
            t += f" • last upload {fmt_ts(self.last_ok)}"
        n = len(self.pending) + len(self.backups_pending) + (1 if store.dirty.is_set() else 0)
        if n:
            t += f" • queue {n}"
        if self.last_error:
            t += f"\n⚠️ {esc(self.last_error[:220])}"
        return t


# ======================================================================
# Telegram layer
# ======================================================================
bot = None   # created in init()

STATE = {}                     # uid -> {"flow":..., "ts":..., "bot_id":...}
SMART = {}                     # uid -> pending file for smart replace
INFLIGHT = set()
INFLIGHT_LOCK = threading.Lock()
TEXT_FLOWS = {"await_repo", "await_env", "await_sched", "await_rename", "await_channel"}
DOC_FLOWS = {"await_upload", "await_replace"}
EXTS = (".py", ".js", ".zip")


def set_flow(uid, flow, **kw):
    STATE[uid] = {"flow": flow, "ts": time.time(), **kw}


def clear_flow(uid):
    STATE.pop(uid, None)


def flow_of(uid):
    st = STATE.get(uid)
    if not st:
        return None
    if time.time() - st["ts"] > FLOW_TTL:
        STATE.pop(uid, None)
        return None
    return st


def is_owner(uid):
    return int(uid) == int(OWNER_ID)


def allowed(uid):
    return is_owner(uid) or int(uid) in ADMIN_IDS


def slot_limit(uid):
    return OWNER_SLOTS if is_owner(uid) else ADMIN_SLOTS


def can_access(b, uid):
    return bool(b) and (int(b.get("owner", 0)) == int(uid) or is_owner(uid))


def KB(rows):
    m = types.InlineKeyboardMarkup()
    for r in rows:
        m.row(*[types.InlineKeyboardButton(t, callback_data=d) for t, d in r])
    return m


def cd(action, bid="", extra=""):
    s = f"h|{action}"
    if bid != "" or extra != "":
        s += f"|{bid}"
    if extra != "":
        s += f"|{extra}"
    return s


def notify(chat_id, text, kb=None):
    try:
        bot.send_message(int(chat_id), text[:4000], parse_mode="HTML", reply_markup=kb)
    except Exception:
        log.warning("notify failed", exc_info=True)


class Ctx:
    def __init__(self, call):
        self.call = call
        self.uid = call.from_user.id
        self.chat = call.message.chat.id
        self.mid = call.message.message_id
        self.done = False

    def ans(self, text=None, alert=False):
        if self.done:
            return
        self.done = True
        try:
            bot.answer_callback_query(self.call.id, text or "", show_alert=alert)
        except Exception:
            pass

    def edit(self, text, kb=None):
        try:
            bot.edit_message_text(text, self.chat, self.mid, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            if "not modified" in str(e).lower():
                return
            log.warning("edit failed: %s", e)
            report_problem(f"button '{getattr(self.call, 'data', '?')}': screen edit nahi hui, naya message bheja", e)
            try:
                bot.send_message(self.chat, text, parse_mode="HTML", reply_markup=kb)
            except Exception as e2:
                log.warning("send fallback failed: %s", e2)
                report_problem(f"button '{getattr(self.call, 'data', '?')}': screen bhej bhi nahi paye", e2)

    def show(self, screen):
        self.edit(*screen)

    def send_doc(self, data, filename):
        bio = io.BytesIO(data)
        bio.name = filename
        bot.send_document(self.chat, bio, visible_file_name=filename)


# ---------------------------------------------------------------- screens
def panel_text():
    return (
        "<b>🤖 CAMADS BOT HOSTING</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Apne Python / Node bots yahan se host aur manage karo.\n\n"
        "📦 Upload: <code>.py</code> / <code>.js</code> / <code>.zip</code> ya Git repo\n"
        "▶️ Start • ⏹ Stop • 🔄 Restart\n"
        "📜 Logs • ⚙️ Config • 📥 Download • 🧬 Clone\n"
        "📝 Replace File • ⏪ Rollback • 🔔 Alerts • 🧠 RAM limit • ⏰ Daily restart\n"
        "💡 Tip: kisi bot ki file seedha bhejo - matching bot replace karne ka option aayega.\n"
        "☁️ Saara data private Telegram channel me save hota hai - redeploy ke baad auto-restore.\n\n"
        f"<i>Hosting bot v{VERSION}</i>"
    )


def screen_panel(uid):
    bots = store.by_owner(uid)
    text = panel_text() + f"\n\n📊 <b>Your Bots:</b> {len(bots)}/{slot_limit(uid)}"
    kb = KB([
        [("➕ Add Bot", cd("add")), ("📂 My Bots", cd("list"))],
        [("📊 Statistics", cd("stats"))],
        [("☁️ Storage Channel" + (" ✅" if sync.enabled else " ⚠️"), cd("channel"))],
    ])
    return text, kb


def screen_list(uid):
    bots = store.by_owner(uid)
    rows = []
    for b in bots:
        icon = "🟢" if status_of(b)["running"] else "🔴"
        rows.append([(f"{icon} {str(b.get('name', 'bot'))[:35]}", cd("view", b["_id"]))])
    rows.append([("➕ Add Bot", cd("add"))])
    if bots:
        rows.append([("▶️ Start All", cd("massgo", "start")),
                     ("⏹ Stop All", cd("massask", "stop")),
                     ("🔄 Restart All", cd("massask", "restart"))])
    rows.append([("🔙 Hosting Panel", cd("panel"))])
    text = f"<b>📂 MY BOTS</b>\n━━━━━━━━━━━━━━━━━━\nSlots: {len(bots)}/{slot_limit(uid)}"
    if not bots:
        text += "\n\n<i>Abhi koi bot nahi. ➕ Add Bot dabao.</i>"
    return text, KB(rows)


def screen_bot(b):
    st = status_of(b)
    bid = b["_id"]
    status = "🟢 Running" if st["running"] else ("💥 Crashed" if b.get("status") == "crashed" else "🔴 Stopped")
    text = (
        f"<b>🤖 {esc(b.get('name', 'bot'))}</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📌 Status: <b>{status}</b>\n"
        f"🐍 Entry: <code>{esc(st['entry'])}</code>\n"
        f"🆔 ID: <code>{bid}</code>\n"
        f"🧠 CPU: <code>{st['cpu']:.1f}%</code>\n"
        f"💾 RAM: <code>{fmt_bytes(st['mem'])}</code>\n"
        f"⏱ Uptime: <code>{fmt_uptime(st['uptime'])}</code>"
    )
    if b.get("last_error"):
        text += f"\n\n⚠️ <b>Last Error</b>\n<pre>{fit_pre(b['last_error'][-1200:], 1200)}</pre>"
    rows = []
    if st["running"]:
        rows.append([("⏹ Stop", cd("stop", bid)), ("🔄 Restart", cd("restart", bid))])
    else:
        rows.append([("▶️ Start", cd("start", bid)), ("🗑️ Delete", cd("delask", bid))])
    ar = "ON" if b.get("auto_restart") else "OFF"
    rows.append([("📝 Replace File", cd("replace", bid)), (f"🔁 Auto-restart: {ar}", cd("ar", bid, "v"))])
    rows.append([("📜 Logs", cd("logs", bid)), ("⚙️ Config", cd("env", bid))])
    rows.append([("📥 Download", cd("download", bid)), ("🧬 Clone", cd("clone", bid))])
    rows.append([(f"⏪ Rollback ({len(get_backups(bid))})", cd("rollback", bid)), ("✏️ Rename", cd("rename", bid))])
    rows.append([("🔙 My Bots", cd("list"))])
    return text, KB(rows)


def screen_logs(b):
    bid = b["_id"]
    logs = read_log_tail(bid) or "(abhi koi output nahi)"
    mods = missing_modules(read_log_tail(bid, max_lines=80), bot_dir(bid))
    rows = pip_rows(bid, mods) if mods else []
    rows.append([("🔄 Refresh", cd("logs", bid)), ("📄 Full Log", cd("logfile", bid))])
    rows.append([("🔙 Bot", cd("view", bid))])
    text = f"<b>📜 LOGS: {esc(b.get('name'))}</b>\n━━━━━━━━━━━━━━━━━━\n<pre>{fit_pre(logs[-2500:], 3200)}</pre>"
    return text, KB(rows)


def pip_rows(bid, mods):
    PIP_SUGGEST[bid] = [pip for _i, pip in mods]
    return [[(f"📦 pip install {pip}", cd("pip", bid, str(i)))] for i, (_imp, pip) in enumerate(mods)]


def screen_env(b):
    bid = b["_id"]
    env = b.get("env") or {}
    keys = list(env.keys())[:20]
    rows = [[(f"🗑 {k}", cd("envdel", bid, str(i)))] for i, k in enumerate(keys)]
    rows.append([("➕ Add / Edit", cd("envadd", bid))])
    rows.append([(f"🔁 Auto-restart: {'ON' if b.get('auto_restart') else 'OFF'}", cd("ar", bid, "e"))])
    rl = int(b.get("ram_limit_mb") or 0)
    rows.append([(f"🔔 Crash alerts: {'ON' if b.get('alerts', True) else 'OFF'}", cd("alerts", bid)),
                 (f"🧠 RAM limit: {str(rl) + ' MB' if rl else 'OFF'}", cd("ram", bid))])
    sch = b.get("daily_restart")
    srow = [(f"⏰ Daily restart: {sch + ' ' + SCHEDULE_TZ_LABEL if sch else 'OFF'}", cd("schedset", bid))]
    if sch:
        srow.append(("✖️ Off", cd("schedoff", bid)))
    rows.append(srow)
    rows.append([("🔙 Bot", cd("view", bid))])
    lines = "\n".join(f"• <code>{esc(k)}</code> = <code>{'*' * min(8, len(str(env[k])))}…</code>" for k in keys)
    text = f"<b>⚙️ CONFIG: {esc(b.get('name'))}</b>\n━━━━━━━━━━━━━━━━━━\n{lines or '<i>Koi variable nahi</i>'}"
    return text, KB(rows)


def screen_rollback(b):
    bid = b["_id"]
    items = get_backups(bid)
    rows, lines = [], []
    for d in items:
        lines.append(f"• {fmt_ts(d['ts'])} ({fmt_bytes(d['size'])}) - {esc(d.get('label', ''))}")
        rows.append([(f"⏪ {fmt_ts(d['ts'])} • {fmt_bytes(d['size'])}", cd("rbpick", bid, str(d["ms"])))])
    rows.append([("🔙 Bot", cd("view", bid))])
    body = "\n".join(lines) if lines else "<i>Abhi koi saved version nahi. Har Replace par copy auto-save hoti hai.</i>"
    text = (f"<b>⏪ ROLLBACK: {esc(b.get('name'))}</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Version tap karo to restore ho jayega (time {SCHEDULE_TZ_LABEL} me). Current version pehle save hota hai.\n\n{body}")
    return text, KB(rows)


def server_stats_text():
    lines = []
    try:
        vm = psutil.virtual_memory()
        lines.append(f"🖥 Server CPU: <b>{psutil.cpu_percent(interval=0.3):.0f}%</b>")
        lines.append(f"🧮 Server RAM: <b>{fmt_bytes(vm.used)} / {fmt_bytes(vm.total)}</b> ({vm.percent:.0f}%)")
    except Exception:
        pass
    try:
        du = shutil.disk_usage(str(DATA_DIR))
        lines.append(f"💿 Disk: <b>{fmt_bytes(du.used)} / {fmt_bytes(du.total)}</b> (free {fmt_bytes(du.free)})")
    except Exception:
        pass
    return "\n".join(lines)


def screen_stats(uid):
    bots = store.by_owner(uid)
    sts = [status_of(b) for b in bots]
    running = sum(1 for s in sts if s["running"])
    text = (
        "<b>📊 HOSTING STATISTICS</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📦 Total Bots: <b>{len(bots)}</b>\n🟢 Running: <b>{running}</b>\n🔴 Stopped: <b>{len(bots) - running}</b>\n"
        f"💾 RAM Used: <b>{fmt_bytes(sum(s['mem'] for s in sts))}</b>\n\n"
        "<b>🖥 SERVER</b>\n" + (server_stats_text() or "<i>no data</i>") + "\n\n" + sync.status_text()
    )
    return text, KB([[("🔙 Hosting Panel", cd("panel"))]])


# ---------------------------------------------------------------- creating / replacing bots
def download_tg(file_id):
    info = bot.get_file(file_id)
    return bot.download_file(info.file_path)


def create_bot_doc(uid, name, root, kind, entry, extra=None):
    count, total = tree_stats(root)
    bid = root.name
    doc = {
        "_id": bid, "owner": int(uid), "name": name, "created": now(), "status": "stopped",
        "kind": kind, "entry": entry, "env": {}, "auto_restart": False, "alerts": True,
        "ram_limit_mb": 0, "daily_restart": None, "backups": [], "last_error": "",
        "files_count": count, "size": total,
    }
    if extra:
        doc.update(extra)
    store.save(doc)
    return doc


def new_bot_id():
    while True:
        bid = secrets.token_hex(4)
        if not bot_dir(bid).exists() and not store.get(bid):
            return bid


def handle_upload(m):
    uid = m.from_user.id
    doc = m.document
    fname = doc.file_name or "bot.py"
    lower = fname.lower()
    if not lower.endswith(EXTS):
        bot.reply_to(m, "❌ Sirf .py, .js ya .zip file bhejo.")
        return
    if doc.file_size and int(doc.file_size) > MAX_UPLOAD_BYTES:
        bot.reply_to(m, f"❌ File bahut badi hai. Max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        return
    if len(store.by_owner(uid)) >= slot_limit(uid):
        clear_flow(uid)
        bot.reply_to(m, "⚠️ Bot slot limit poori ho gayi.")
        return
    tmp = Path(tempfile.mkdtemp(prefix="camads_host_"))
    bid = None
    try:
        raw = download_tg(doc.file_id)
        project = tmp / "project"
        if lower.endswith(".zip"):
            extract_zip(raw, project)
            flatten_single_dir(project)
        else:
            project.mkdir()
            (project / safe_name(fname)).write_bytes(raw)
        kind, entry = entry_for_dir(project)
        if not kind:
            raise ValueError("Entry file nahi mili. bot.py/main.py/app.py/index.js use karo ya ZIP me rakho.")
        if kind == "py" and not lower.endswith(".zip"):
            try:
                compile(raw, fname, "exec")
            except SyntaxError as e:
                raise ValueError(f"Syntax error {fname}, line {e.lineno}: {e.msg}")
        bid = new_bot_id()
        root = bot_dir(bid)
        shutil.copytree(project, root, dirs_exist_ok=True)
        d = create_bot_doc(uid, safe_name(Path(fname).stem), root, kind, entry, {"upload_bytes": len(raw)})
        sync.mark_bot(bid)
        clear_flow(uid)
        bot.send_message(
            m.chat.id,
            f"✅ <b>Bot upload ho gaya!</b>\n\n📦 Name: <code>{esc(d['name'])}</code>\n"
            f"📄 Files: <code>{d['files_count']}</code>\n🐍 Entry: <code>{esc(entry)}</code>\n🆔 ID: <code>{bid}</code>\n\n"
            "⚙️ Bot ko token/keys chahiye to Config me <code>KEY=value</code> add karo, fir Start.",
            parse_mode="HTML",
            reply_markup=KB([[("▶️ Start Now", cd("start", bid))], [("⚙️ Config", cd("env", bid))],
                             [("📂 My Bots", cd("list"))]]))
    except zipfile.BadZipFile:
        if bid:
            shutil.rmtree(bot_dir(bid), ignore_errors=True)
        bot.reply_to(m, "❌ ZIP corrupted/invalid hai.")
    except Exception as e:
        if bid:
            shutil.rmtree(bot_dir(bid), ignore_errors=True)
        bot.reply_to(m, f"❌ Upload failed: <code>{esc(e)}</code>", parse_mode="HTML")
    finally:
        clear_flow(uid)
        shutil.rmtree(tmp, ignore_errors=True)


def handle_repo(m, url):
    uid = m.from_user.id
    url = (url or "").strip()
    if not re.fullmatch(r"https?://[^\s]+", url, flags=re.I):
        bot.reply_to(m, "❌ Galat URL. Public https:// Git repo URL bhejo ya /cancel.")
        return
    if not any(h in url.lower() for h in ("github.com/", "gitlab.com/", "bitbucket.org/")):
        bot.reply_to(m, "❌ Sirf GitHub / GitLab / Bitbucket URLs supported hain.")
        return
    if len(store.by_owner(uid)) >= slot_limit(uid):
        clear_flow(uid)
        bot.reply_to(m, "⚠️ Bot slot limit poori ho gayi.")
        return
    tmp = Path(tempfile.mkdtemp(prefix="camads_git_"))
    bid = None
    try:
        bot.send_message(m.chat.id, "⏳ Repo clone ho raha hai...")
        project = tmp / "project"
        r = subprocess.run(["git", "clone", "--depth", "1", url, str(project)], capture_output=True, text=True,
                           timeout=180, stdin=subprocess.DEVNULL, cwd=str(tmp),
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "git clone failed")[-900:])
        shutil.rmtree(project / ".git", ignore_errors=True)
        kind, entry = entry_for_dir(project)
        if not kind:
            raise ValueError("Entry file nahi mili (bot.py/main.py/app.py/index.js ...).")
        bid = new_bot_id()
        root = bot_dir(bid)
        shutil.copytree(project, root, dirs_exist_ok=True)
        name = safe_name(Path(url.rstrip("/")).stem.replace(".git", "") or "git_bot")
        create_bot_doc(uid, name, root, kind, entry, {"repo_url": url})
        sync.mark_bot(bid)
        clear_flow(uid)
        bot.send_message(
            m.chat.id,
            f"✅ <b>Repo deploy ho gaya!</b>\n\n📦 Name: <code>{esc(name)}</code>\n📄 Entry: <code>{esc(entry)}</code>\n"
            f"🆔 ID: <code>{bid}</code>\n\n⚙️ Config me <code>KEY=value</code> variables add kar sakte ho.",
            parse_mode="HTML",
            reply_markup=KB([[("▶️ Start Now", cd("start", bid))], [("⚙️ Config", cd("env", bid))],
                             [("📂 My Bots", cd("list"))]]))
    except FileNotFoundError:
        bot.reply_to(m, "❌ Server par git installed nahi hai.")
    except Exception as e:
        if bid:
            shutil.rmtree(bot_dir(bid), ignore_errors=True)
        bot.reply_to(m, f"❌ Repo deploy failed: <code>{esc(e)}</code>", parse_mode="HTML")
    finally:
        clear_flow(uid)
        shutil.rmtree(tmp, ignore_errors=True)


def do_replace(uid, bid, fname, raw):
    """-> (ok, text, kb). Validates first; running bot untouched on failure."""
    b = store.get(bid)
    lower = (fname or "bot.py").lower()
    if not lower.endswith(EXTS):
        return False, "❌ Sirf .py, .js ya .zip file accepted hai.", None
    if len(raw) > MAX_UPLOAD_BYTES:
        return False, "❌ File bahut badi hai.", None
    tmp = Path(tempfile.mkdtemp(prefix="camads_repl_"))
    swapped, was_running, backup_ms = False, False, None
    try:
        root = bot_dir(bid)
        staged = tmp / "staged"
        if lower.endswith(".zip"):
            extract_zip(raw, staged)
            flatten_single_dir(staged)
            kind, entry = entry_for_dir(staged)
            if not kind:
                raise ValueError("ZIP me entry file nahi mili (bot.py/main.py/app.py/index.js ...).")
        else:
            kind = "py" if lower.endswith(".py") else "js"
            old_kind, old_entry = b.get("kind"), b.get("entry")
            if not (old_entry and (root / old_entry).is_file()):
                old_kind, old_entry = entry_for_dir(root) if root.exists() else (None, None)
            if old_kind and old_kind != kind:
                raise ValueError("Ye %s bot hai, aapne %s file bheji. Matching file ya ZIP bhejo." % (
                    "Python" if old_kind == "py" else "Node.js", "Python" if kind == "py" else "Node.js"))
            entry = old_entry if old_kind else safe_name(fname)
            if kind == "py":
                try:
                    compile(raw, fname, "exec")
                except SyntaxError as e:
                    raise ValueError(f"Syntax error {fname}, line {e.lineno}: {e.msg}")
            if root.exists():
                shutil.copytree(root, staged, ignore=shutil.ignore_patterns(*RUNTIME_IGNORE))
            else:
                staged.mkdir(parents=True)
            target = staged / safe_rel(entry)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        files_count, _tot = tree_stats(staged)
        diff_text = diff_summary(root, staged)

        with BLOCKS[bid]:
            try_adopt(bid, entry, kind)
            was_running = status_of(b)["running"]
            stop_bot(bid, True)
            backup_ms = make_backup(bid, f"before replace: {fname}")
            swapped = True
            swap_project(root, staged)
            write_dotenv(root, b.get("env") or {})
            append_log(bid, f"=== FILE REPLACED {now()} ({fname}) ===")
            store.update(bid, kind=kind, entry=entry, files_count=files_count, last_error="", last_replaced=now())
            sync.mark_bot(bid)
            RESTARTS.pop(bid, None)
            if was_running:
                res = start_bot(bid)
                if res.get("ok"):
                    note = "▶️ Bot naye file ke saath restart ho gaya."
                    if backup_ms and bid in PROCS:
                        PROCS[bid]["rollback_to"] = backup_ms
                        note += f"\n🛡 Auto-rollback {AUTO_ROLLBACK_SECONDS}s ke liye ON: crash hua to purana version wapas aayega."
                else:
                    store.update(bid, last_error=res.get("error", "start failed"), status="crashed")
                    note = "⚠️ File replace ho gayi, par bot start nahi hua. Logs / Last Error dekho."
            else:
                note = "⏹ Bot band tha. Naya file chalane ke liye Start dabao."
        rows = [[("🤖 Open Bot", cd("view", bid)), ("📜 Logs", cd("logs", bid))]]
        if backup_ms:
            rows.append([("⏪ Undo this replace", cd("rbpick", bid, str(backup_ms)))])
        text = (f"✅ <b>File replace ho gayi!</b>\n\n📦 Name: <code>{esc(b.get('name'))}</code>\n"
                f"🐍 Entry: <code>{esc(entry)}</code>\n📄 Files: <code>{files_count}</code>\n\n"
                f"🔍 <b>Changes</b>\n{esc(diff_text)}\n\n{note}")
        return True, text, KB(rows)
    except zipfile.BadZipFile:
        return False, "❌ ZIP corrupted/invalid hai.", None
    except Exception as e:
        tail = " (purana bot untouched)"
        if swapped:
            tail = " Restore fail - Rollback use karo."
            if backup_ms:
                try:
                    r = restore_backup(bid, backup_ms, make_new_backup=False)
                    if r.get("ok"):
                        tail = " Purana version restore ho gaya."
                        if was_running:
                            start_bot(bid)
                except Exception:
                    pass
        return False, f"❌ Replace failed{tail}: <code>{esc(e)}</code>", None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def match_bots(uid, fname):
    def norm(s):
        return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())

    def norm_file(f):
        stem = os.path.splitext(os.path.basename(str(f or "")))[0]
        return norm(re.sub(r"[\s_-]*\(\d+\)$", "", stem))

    key = norm_file(fname)
    if not key:
        return []
    strong, weak = [], []
    for b in store.by_owner(uid):
        cands = {norm(b.get("name")), norm_file(b.get("entry"))} - {""}
        if key in cands:
            strong.append(b["_id"])
        elif len(key) >= 8 and any(len(c) >= 8 and (key.startswith(c) or c.startswith(key)) for c in cands):
            weak.append(b["_id"])
    return strong or weak


# ---------------------------------------------------------------- storage-channel setup (from the bot)
def set_channel_global(cid):
    global CHANNEL_ID
    CHANNEL_ID = int(cid or 0)
    sync.enabled = bool(CHANNEL_ID)


def _read_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_channel_config(cid):
    """config.json + owner ki private chat me PINNED message (disk wipe ke baad bhi channel yaad rahe)."""
    cfg = _read_config()
    cfg["channel_id"] = int(cid)
    old_msg = cfg.get("cfg_msg")
    try:
        m = bot.send_message(OWNER_ID, "☁️ <b>Storage channel config</b> (pinned - delete mat karna)\n"
                                       f"<code>#config CHANNEL_ID={int(cid)}</code>", parse_mode="HTML")
        try:
            bot.pin_chat_message(OWNER_ID, m.message_id, disable_notification=True)
        except Exception:
            log.warning("config pin failed", exc_info=True)
        cfg["cfg_msg"] = m.message_id
        if old_msg:
            try:
                bot.delete_message(OWNER_ID, old_msg)
            except Exception:
                pass
    except Exception:
        log.warning("config message failed", exc_info=True)
    try:
        CONFIG_FILE.write_text(json.dumps(cfg), encoding="utf-8")
    except Exception:
        pass


def find_saved_channel():
    """-> int (0 = bewajah band kiya gaya) ya None (kuch saved nahi)."""
    cfg = _read_config()
    if "channel_id" in cfg:
        return int(cfg["channel_id"])
    try:
        pm = getattr(bot.get_chat(OWNER_ID), "pinned_message", None)
        txt = (getattr(pm, "text", None) or "") if pm else ""
        mm = re.search(r"#config CHANNEL_ID=(-?\d+)", txt)
        if mm:
            return int(mm.group(1))
    except Exception:
        pass
    return None


def apply_channel(cid):
    """Naya channel on karo: purane channel ke file_ids reset, restore (agar channel me DB hai), sab upload queue."""
    prev = CHANNEL_ID
    set_channel_global(cid)
    if prev and prev != cid:
        for b in store.all():
            def _reset(d):
                d["tg_msg"] = d["tg_file"] = d["tg_hash"] = None
                for x in d.get("backups", []):
                    x.pop("tg_msg", None)
                    x.pop("tg_file", None)
            store.mutate(b["_id"], _reset)
        sync.db_msg_id = None
    save_channel_config(cid)
    n_db = n_files = 0
    try:
        n_db, n_files = sync.restore()
    except Exception:
        log.exception("restore after channel set failed")
    for b in store.all():
        sync.mark_bot(b["_id"])
        for x in b.get("backups", []):
            sync.mark_backup(b["_id"], x["ms"])
    store.dirty.set()
    sync.ensure_thread()
    sync.wake.set()
    return n_db, n_files


def disable_channel():
    set_channel_global(0)
    save_channel_config(0)


def fwd_chat(m):
    fc = getattr(m, "forward_from_chat", None)
    if fc is not None:
        return fc
    return getattr(getattr(m, "forward_origin", None), "chat", None)


def handle_channel_input(m):
    uid = m.from_user.id
    if not is_owner(uid):
        return
    cid = None
    fc = fwd_chat(m)
    try:
        if fc is not None:
            cid = int(fc.id)
        else:
            t = (getattr(m, "text", None) or "").strip()
            if re.fullmatch(r"-?\d{5,}", t):
                cid = int(t)
                if cid > 0:
                    cid = int("-100" + t)      # "1234567890" -> -1001234567890
            elif re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", t):
                cid = int(bot.get_chat("@" + t.lstrip("@")).id)
    except Exception as e:
        bot.reply_to(m, f"❌ Channel nahi mila: <code>{esc(e)}</code>", parse_mode="HTML")
        return
    if cid is None:
        bot.reply_to(m, "❌ Channel ka koi message FORWARD karo, ya ID (<code>-100...</code>) / <code>@username</code> bhejo. /cancel",
                     parse_mode="HTML")
        return
    err = sync.check(cid)
    if err:
        bot.reply_to(m, f"❌ <b>Channel set nahi hua</b>\n<code>{esc(err)}</code>\n\nSahi karke dobara bhejo ya /cancel.",
                     parse_mode="HTML")
        return
    wait = bot.reply_to(m, "⏳ Channel connect ho raha hai, data sync ho raha hai...")
    try:
        n_db, n_files = apply_channel(cid)
        clear_flow(uid)
        extra = ""
        if n_db or n_files:
            extra = f"\n♻️ Channel me purana data mila: DB {n_db} bot(s), files {n_files} bot(s) restore kiye."
        bot.edit_message_text(f"✅ <b>Storage channel set ho gaya!</b>\n🆔 <code>{cid}</code>{extra}\n\n"
                              "Ab saara data (DB, bot files, backups) is channel me save hoga.",
                              m.chat.id, wait.message_id, parse_mode="HTML",
                              reply_markup=KB([[("☁️ Channel Settings", cd("channel"))], [("🔙 Panel", cd("panel"))]]))
    except Exception as e:
        log.exception("apply_channel failed")
        bot.edit_message_text(f"❌ Channel set karte waqt error: <code>{esc(e)}</code>", m.chat.id, wait.message_id,
                              parse_mode="HTML")


def screen_channel(uid):
    if sync.enabled:
        head = f"🆔 Channel: <code>{CHANNEL_ID}</code>\n{sync.status_text()}"
    else:
        head = sync.status_text()
    text = (
        "<b>☁️ STORAGE CHANNEL</b>\n━━━━━━━━━━━━━━━━━━\n" + head + "\n\n"
        "<b>Kaise set karein:</b>\n"
        "1️⃣ Ek <b>private</b> channel banao\n"
        "2️⃣ Is bot ko channel ka <b>Admin</b> banao (Post messages + Edit/Pin messages)\n"
        "3️⃣ Channel me koi message post karo aur use <b>yahan forward</b> karo\n"
        "    (ya channel ID <code>-100...</code> / <code>@username</code> bhejo)"
    )
    rows = [[("✏️ Set / Change Channel" if sync.enabled else "✏️ Set Channel", cd("setch"))]]
    if sync.enabled:
        rows.append([("☁️ Sync Now", cd("syncnow"))])
        rows.append([("🚫 Disable Sync", cd("chaskoff"))])
    rows.append([("🔙 Hosting Panel", cd("panel"))])
    return text, KB(rows)


# ---------------------------------------------------------------- command handlers
def register_handlers():
    @bot.message_handler(commands=["start", "help"])
    def cmd_start(m):
        if not allowed(m.from_user.id):
            bot.reply_to(m, "⛔ Ye private hosting bot hai.")
            return
        bot.send_message(m.chat.id, panel_text(), parse_mode="HTML",
                         reply_markup=KB([[("🚀 Open Hosting Panel", cd("panel"))]]))

    @bot.message_handler(commands=["hosting", "panel"])
    def cmd_hosting(m):
        if not allowed(m.from_user.id):
            bot.reply_to(m, "⛔ Ye private hosting bot hai.")
            return
        text, kb = screen_panel(m.from_user.id)
        bot.send_message(m.chat.id, text, parse_mode="HTML", reply_markup=kb)

    @bot.message_handler(commands=["cancel"])
    def cmd_cancel(m):
        clear_flow(m.from_user.id)
        SMART.pop(m.from_user.id, None)
        bot.reply_to(m, "✅ Hosting action cancel ho gaya.")

    @bot.message_handler(commands=["errors", "lasterror"])
    def cmd_errors(m):
        if not is_owner(m.from_user.id):
            return
        if not ERROR_LOG:
            bot.reply_to(m, "✅ Abhi tak koi problem record nahi hui.")
            return
        rows = []
        for r in list(ERROR_LOG)[-10:][::-1]:
            rows.append(f"<b>#{r['n']}</b> {esc(fmt_ts(r['ts']))} - {esc(r['title'])}\n"
                        f"   <i>{esc(r['who'])}</i> | <code>{esc(r['err'][:120])}</code>")
        bot.send_message(m.chat.id, "<b>🧾 LAST PROBLEMS</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n\n".join(rows),
                         parse_mode="HTML")

    @bot.message_handler(commands=["diag", "health"])
    def cmd_diag(m):
        if not is_owner(m.from_user.id):
            return
        out = ["<b>🩺 HEALTH CHECK</b>", "━━━━━━━━━━━━━━━━━━"]
        t0 = time.time()
        try:
            me = bot.get_me()
            out.append(f"✅ Telegram API: @{esc(me.username)} ({int((time.time() - t0) * 1000)} ms)")
        except Exception as e:
            out.append(f"❌ Telegram API: {esc(str(e)[:150])}")
        try:
            wh = bot.get_webhook_info()
            line = f"📬 Pending updates: {wh.pending_update_count}"
            if getattr(wh, "url", ""):
                line += f" | ⚠️ webhook set hai: {esc(wh.url)}"
            out.append(line)
        except Exception as e:
            out.append(f"⚠️ Webhook info nahi mili: {esc(str(e)[:100])}")
        if sync.enabled:
            try:
                err = sync.check()
                out.append("✅ Storage channel OK" if not err else f"❌ Storage channel: {esc(err.splitlines()[0])}")
            except Exception as e:
                out.append(f"❌ Storage channel check fail: {esc(str(e)[:100])}")
        else:
            out.append("⚠️ Storage channel set nahi hai")
        try:
            du = shutil.disk_usage(DATA_DIR)
            ok = du.free > 200 * 1024 * 1024
            out.append(f"{'✅' if ok else '❌'} Disk free: {du.free // 1048576} MB / {du.total // 1048576} MB")
        except Exception as e:
            out.append(f"⚠️ Disk check fail: {esc(str(e)[:100])}")
        try:
            vm = psutil.virtual_memory()
            proc = psutil.Process()
            out.append(f"{'✅' if vm.percent < 90 else '❌'} Server RAM: {vm.percent:.0f}% used "
                       f"({vm.available // 1048576} MB free) | hosting bot: {proc.memory_info().rss // 1048576} MB")
            up = int(time.time() - proc.create_time())
            out.append(f"⏱ Uptime: {up // 3600}h {up % 3600 // 60}m | Python {sys.version.split()[0]} | v{VERSION}")
        except Exception as e:
            out.append(f"⚠️ RAM check fail: {esc(str(e)[:100])}")
        out.append(f"🧾 Recorded problems: {len(ERROR_LOG)}  (/errors se dekho)")
        bot.send_message(m.chat.id, "\n".join(out), parse_mode="HTML")

    @bot.message_handler(content_types=["document"])
    def on_document(m):
        uid = m.from_user.id
        if not allowed(uid) or m.chat.type != "private":
            return
        st = flow_of(uid)
        doc = m.document
        fname = doc.file_name or "bot.py"
        try:
            if st and st["flow"] == "await_channel":
                handle_channel_input(m)
                return
            if st and st["flow"] == "await_upload":
                handle_upload(m)
                return
            if st and st["flow"] == "await_replace":
                b = store.get(st.get("bot_id"))
                if not can_access(b, uid):
                    clear_flow(uid)
                    bot.reply_to(m, "❌ Bot nahi mila.")
                    return
                if doc.file_size and int(doc.file_size) > MAX_UPLOAD_BYTES:
                    bot.reply_to(m, "❌ File bahut badi hai.")
                    return
                wait = bot.reply_to(m, "⏳ Replace ho raha hai...")
                ok, text, kb = do_replace(uid, b["_id"], fname, download_tg(doc.file_id))
                if ok:
                    clear_flow(uid)
                else:
                    text += "\n\nFixed file bhejo ya /cancel."
                bot.edit_message_text(text, m.chat.id, wait.message_id, parse_mode="HTML", reply_markup=kb)
                return
            # no flow: smart replace
            if fname.lower().endswith(EXTS):
                matches = match_bots(uid, fname)
                if matches:
                    SMART[uid] = {"file_id": doc.file_id, "file_name": fname, "file_size": doc.file_size}
                    rows = []
                    for bid in matches[:6]:
                        b = store.get(bid)
                        if b:
                            icon = "🟢" if status_of(b)["running"] else "🔴"
                            rows.append([(f"{icon} Replace: {str(b.get('name', 'bot'))[:30]}", cd("srgo", bid))])
                    rows.append([("📂 Doosra bot chuno", cd("srlist"))])
                    rows.append([("❌ Cancel", cd("srno"))])
                    bot.send_message(
                        m.chat.id,
                        f"⚡ <b>SMART REPLACE</b>\n<code>{esc(fname)}</code> neeche wale bot ka update lagta hai.\n"
                        "Tap karo: running bot auto-restart hoga, purana version Rollback ke liye save rehta hai.",
                        parse_mode="HTML", reply_markup=KB(rows))
                    return
            bot.reply_to(m, "ℹ️ Naya bot add karne ke liye /hosting → ➕ Add Bot → Upload File.")
        except Exception as e:
            log.exception("document handler failed")
            bot.reply_to(m, f"❌ Error: {e}")

    @bot.message_handler(content_types=["photo", "video", "audio", "voice", "sticker", "animation", "video_note"],
                         func=lambda m: allowed(m.from_user.id) and (flow_of(m.from_user.id) or {}).get("flow") == "await_channel")
    def on_media_for_channel(m):
        handle_channel_input(m)

    @bot.message_handler(content_types=["text"], func=lambda m: not (m.text or "").startswith("/"))
    def on_text(m):
        uid = m.from_user.id
        if not allowed(uid):
            return
        st = flow_of(uid)
        if not st or st["flow"] not in TEXT_FLOWS:
            return
        text = (m.text or "").strip()
        flow = st["flow"]
        try:
            if flow == "await_channel":
                handle_channel_input(m)
                return
            if flow == "await_repo":
                handle_repo(m, text)
                return
            bid = st.get("bot_id")
            b = store.get(bid) if bid else None
            if flow == "await_env":
                if "=" not in text:
                    bot.reply_to(m, "❌ Format: KEY=value")
                    return
                key, value = text.split("=", 1)
                key = key.strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    bot.reply_to(m, "❌ Variable name galat hai.")
                    return
                if len(key) > MAX_ENV_KEY_LEN:
                    bot.reply_to(m, f"❌ Name bahut lamba (max {MAX_ENV_KEY_LEN}).")
                    return
                if not can_access(b, uid):
                    clear_flow(uid)
                    bot.reply_to(m, "❌ Bot nahi mila.")
                    return
                env = dict(b.get("env") or {})
                env[key] = value
                store.update(bid, env=env)
                write_dotenv(bot_dir(bid), env)
                clear_flow(uid)
                try:      # secret wala message chat se hata do
                    bot.delete_message(m.chat.id, m.message_id)
                except Exception:
                    pass
                bot.send_message(m.chat.id, f"✅ <code>{esc(key)}</code> save ho gaya. Apply karne ke liye bot restart karo.",
                                 parse_mode="HTML", reply_markup=KB([[("⚙️ Config", cd("env", bid))]]))
                return
            if flow == "await_rename":
                if not can_access(b, uid):
                    clear_flow(uid)
                    bot.reply_to(m, "❌ Bot nahi mila.")
                    return
                new = safe_name(text, "") if text else ""
                if not new or len(text) > 60:
                    bot.reply_to(m, "❌ Valid naam bhejo (letters, numbers, . _ -), max 60. /cancel")
                    return
                old = b.get("name")
                store.update(bid, name=new)
                clear_flow(uid)
                bot.send_message(m.chat.id, f"✅ Renamed <code>{esc(old)}</code> → <code>{esc(new)}</code>",
                                 parse_mode="HTML", reply_markup=KB([[("🤖 Open Bot", cd("view", bid))]]))
                return
            if flow == "await_sched":
                if not can_access(b, uid):
                    clear_flow(uid)
                    bot.reply_to(m, "❌ Bot nahi mila.")
                    return
                t = text.lower()
                if t in ("off", "0", "none", "no"):
                    value = None
                else:
                    mm = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
                    if not mm or int(mm.group(1)) > 23 or int(mm.group(2)) > 59:
                        bot.reply_to(m, f"❌ Time HH:MM ({SCHEDULE_TZ_LABEL}, 24h) me bhejo, jaise 04:30 - ya 'off'.")
                        return
                    value = f"{int(mm.group(1)):02d}:{mm.group(2)}"
                store.update(bid, daily_restart=value, last_sched_day=None)
                clear_flow(uid)
                bot.send_message(m.chat.id,
                                 f"⏰ Daily restart: <b>{value + ' ' + SCHEDULE_TZ_LABEL if value else 'OFF'}</b>",
                                 parse_mode="HTML", reply_markup=KB([[("⚙️ Config", cd("env", bid))]]))
                return
        except Exception as e:
            log.exception("text handler failed")
            bot.reply_to(m, f"❌ Error: {e}")

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("h|"))
    def on_callback(call):
        if not call.message:
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            return
        ctx = Ctx(call)
        key = (ctx.uid, call.data, ctx.mid)
        try:
            if not allowed(ctx.uid):
                ctx.ans("Private hosting panel", True)
                return
            with INFLIGHT_LOCK:
                if key in INFLIGHT:
                    ctx.ans("⏳ Abhi kaam chal raha hai...")
                    return
                INFLIGHT.add(key)
            try:
                route(ctx, call.data)
            finally:
                with INFLIGHT_LOCK:
                    INFLIGHT.discard(key)
        except Exception as e:
            log.exception("callback failed: %s", call.data)
            ctx.ans(f"Error: {e}"[:180], True)
        finally:
            ctx.ans()


# ---------------------------------------------------------------- callback router
NO_BOT_ACTIONS = {"panel", "add", "src_file", "src_repo", "list", "stats", "srno", "srlist", "massask", "massgo", "cancel"}


def route(ctx, data):
    p = data.split("|")
    act = p[1] if len(p) > 1 else ""
    bid = p[2] if len(p) > 2 else ""
    extra = p[3] if len(p) > 3 else ""
    uid = ctx.uid

    if act == "panel":
        clear_flow(uid)
        ctx.show(screen_panel(uid))
        return
    if act == "cancel":
        clear_flow(uid)
        ctx.show(screen_panel(uid))
        return
    if act == "add":
        if len(store.by_owner(uid)) >= slot_limit(uid):
            ctx.edit(f"⚠️ <b>Bot slot limit poori.</b>\nCurrent: {len(store.by_owner(uid))}/{slot_limit(uid)}",
                     KB([[("🔙 Back", cd("panel"))]]))
            return
        ctx.edit("<b>➕ ADD HOSTED BOT</b>\n━━━━━━━━━━━━━━━━━━\nKaise deploy karna hai?\n\n"
                 "📄 <b>File:</b> .py / .js / .zip\n🔗 <b>Git:</b> public GitHub/GitLab repo URL",
                 KB([[("📄 Upload .py/.js/.zip", cd("src_file"))], [("🔗 Deploy Git Repo", cd("src_repo"))],
                     [("🔙 Back", cd("panel"))]]))
        return
    if act == "src_file":
        set_flow(uid, "await_upload")
        ctx.edit("<b>📄 UPLOAD BOT FILE</b>\n━━━━━━━━━━━━━━━━━━\n"
                 "<code>.py</code>, <code>.js</code> ya <code>.zip</code> ko Telegram <b>document</b> ki tarah bhejo.\n\n"
                 "ZIP me entry: <code>bot.py</code>, <code>main.py</code>, <code>app.py</code>, <code>index.js</code> ...\n"
                 f"Max size: <b>{MAX_UPLOAD_BYTES // (1024 * 1024)} MB</b>.  /cancel to abort.",
                 KB([[("🔙 Back", cd("add"))]]))
        return
    if act == "src_repo":
        set_flow(uid, "await_repo")
        ctx.edit("<b>🔗 DEPLOY GIT REPOSITORY</b>\n━━━━━━━━━━━━━━━━━━\nPublic Git repo URL bhejo.\n\n"
                 "Example: <code>https://github.com/user/project.git</code>\n"
                 "Repo clone hoga, entry file detect hogi, <code>requirements.txt</code>/<code>package.json</code> "
                 "start par install honge.\n/cancel to abort.",
                 KB([[("🔙 Back", cd("add"))]]))
        return
    if act == "list":
        ctx.show(screen_list(uid))
        return
    if act == "stats":
        ctx.show(screen_stats(uid))
        return
    if act == "syncnow":
        if not is_owner(uid):
            ctx.ans("Sirf owner", True)
            return
        if not sync.enabled:
            ctx.ans("CHANNEL_ID set nahi hai.", True)
            return
        ctx.ans("☁️ Channel me upload ho raha hai...")
        sync.flush_all()
        ctx.show(screen_channel(uid))
        return
    if act == "channel":
        if not is_owner(uid):
            ctx.ans("Sirf owner", True)
            return
        clear_flow(uid)
        ctx.show(screen_channel(uid))
        return
    if act == "setch":
        if not is_owner(uid):
            ctx.ans("Sirf owner", True)
            return
        set_flow(uid, "await_channel")
        ctx.edit("<b>✏️ SET STORAGE CHANNEL</b>\n━━━━━━━━━━━━━━━━━━\n"
                 "Bot ko pehle channel ka <b>Admin</b> banao, phir:\n"
                 "• channel ka koi message yahan <b>forward</b> karo, ya\n"
                 "• channel ID (<code>-100...</code>) / <code>@username</code> bhejo.\n\n/cancel to abort.",
                 KB([[("🔙 Back", cd("channel"))]]))
        return
    if act == "chaskoff":
        if not is_owner(uid):
            ctx.ans("Sirf owner", True)
            return
        ctx.edit("<b>🚫 Channel sync band karein?</b>\nData sirf local disk par rahega (redeploy par ud sakta hai). "
                 "Channel me purani files delete nahi hongi.",
                 KB([[("✅ Haan, band karo", cd("choff"))], [("🔙 Cancel", cd("channel"))]]))
        return
    if act == "choff":
        if not is_owner(uid):
            ctx.ans("Sirf owner", True)
            return
        disable_channel()
        ctx.show(screen_channel(uid))
        return

    # ---- smart replace
    if act == "srno":
        SMART.pop(uid, None)
        ctx.edit("❌ Cancelled.")
        return
    if act == "srlist":
        if uid not in SMART:
            ctx.ans("Expired - file dobara bhejo.", True)
            return
        rows = [[(f"{'🟢' if status_of(x)['running'] else '🔴'} {str(x.get('name', 'bot'))[:32]}", cd("srgo", x["_id"]))]
                for x in store.by_owner(uid)[:30]]
        rows.append([("❌ Cancel", cd("srno"))])
        ctx.edit("📂 <b>Kaunsa bot replace karna hai?</b>", KB(rows))
        return

    # ---- mass actions
    if act == "massask":
        mode = bid
        label = {"stop": "Saare running bots STOP karein", "restart": "Saare running bots RESTART karein"}.get(mode)
        if not label:
            return
        ctx.edit(f"<b>⚠️ {label}?</b>", KB([[("✅ Haan", cd("massgo", mode))], [("🔙 Cancel", cd("list"))]]))
        return
    if act == "massgo":
        mode = bid
        if mode not in ("start", "stop", "restart"):
            return
        ctx.ans("⏳ Kaam chal raha hai...")
        done, skipped, failed = 0, 0, []
        for x in store.by_owner(uid):
            running = status_of(x)["running"]
            try:
                if mode == "start":
                    if running:
                        skipped += 1
                        continue
                    res = start_bot(x["_id"])
                elif mode == "stop":
                    if not running:
                        skipped += 1
                        continue
                    stop_bot(x["_id"])
                    res = {"ok": True}
                else:
                    if not running:
                        skipped += 1
                        continue
                    res = restart_bot(x["_id"])
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            if res.get("ok"):
                done += 1
            else:
                store.update(x["_id"], last_error=res.get("error", "failed"), status="crashed")
                failed.append(f"• {esc(x.get('name'))}: {esc(str(res.get('error'))[:80])}")
        verb = {"start": "Started", "stop": "Stopped", "restart": "Restarted"}[mode]
        text = f"<b>✅ {verb}: {done}</b> • skipped: {skipped}"
        if failed:
            text += f"\n\n⚠️ <b>Failed ({len(failed)})</b>\n" + "\n".join(failed[:10])
        ctx.edit(text, KB([[("🔙 My Bots", cd("list"))]]))
        return

    # ---- everything below needs a bot the user may access
    b = store.get(bid) if bid else None
    if not can_access(b, uid):
        ctx.ans("Bot nahi mila / permission denied", True)
        return

    if act == "view":
        ctx.show(screen_bot(b))
        return

    if act == "start":
        ctx.ans("⏳ Starting…")
        res = start_bot(bid)
        if not res["ok"]:
            store.update(bid, last_error=res["error"], status="crashed")
        ctx.show(screen_bot(store.get(bid)))
        return

    if act == "stop":
        ctx.ans("⏳ Stopping…")
        stop_bot(bid)
        ctx.show(screen_bot(store.get(bid)))
        return

    if act == "restart":
        ctx.ans("⏳ Restarting…")
        res = restart_bot(bid)
        if not res["ok"]:
            store.update(bid, last_error=res["error"], status="crashed")
        ctx.show(screen_bot(store.get(bid)))
        return

    if act == "logs":
        ctx.show(screen_logs(b))
        return

    if act == "logfile":
        try:
            data_b = log_path(bid).read_bytes()
        except Exception:
            data_b = b""
        if not data_b:
            ctx.ans("Abhi koi log nahi.", True)
            return
        ctx.send_doc(data_b, f"{safe_name(b.get('name'), 'bot')}.log")
        return

    if act == "pip":
        sug = PIP_SUGGEST.get(bid) or []
        try:
            pipname = sug[int(extra)]
        except Exception:
            ctx.ans("Expired - Logs dobara kholo.", True)
            return
        ctx.ans("📦 Installing...")
        ctx.edit(f"📦 <code>{esc(pipname)}</code> install ho raha hai ...")
        err = pip_install_one(bot_dir(bid), pipname)
        if err:
            ctx.edit(f"❌ pip failed:\n<pre>{fit_pre(err, 1500)}</pre>", KB([[("📜 Logs", cd("logs", bid))]]))
            return
        sync.mark_bot(bid)
        ctx.edit(f"✅ <code>{esc(pipname)}</code> install ho gaya. Use karne ke liye bot restart karo.",
                 KB([[("🔄 Restart", cd("restart", bid)), ("📜 Logs", cd("logs", bid))]]))
        return

    if act == "env":
        ctx.show(screen_env(b))
        return

    if act == "envadd":
        set_flow(uid, "await_env", bot_id=bid)
        ctx.edit("⚙️ <b>Variable bhejo</b>\nFormat: <code>KEY=value</code>\n(Message save hote hi chat se delete ho jayega.)\n/cancel to abort.",
                 KB([[("🔙 Cancel", cd("env", bid))]]))
        return

    if act == "envdel":
        env = dict(b.get("env") or {})
        keys = list(env.keys())[:20]
        try:
            env.pop(keys[int(extra)], None)
        except Exception:
            ctx.ans("Expired", True)
            return
        store.update(bid, env=env)
        write_dotenv(bot_dir(bid), env)
        ctx.ans("Deleted. Restart karo apply ke liye.")
        ctx.show(screen_env(store.get(bid)))
        return

    if act == "ar":
        nb = store.update(bid, auto_restart=not b.get("auto_restart", False))
        ctx.ans("Auto-restart " + ("ON" if nb["auto_restart"] else "OFF"))
        ctx.show(screen_env(nb) if extra == "e" else screen_bot(nb))
        return

    if act == "alerts":
        nb = store.update(bid, alerts=not b.get("alerts", True))
        ctx.show(screen_env(nb))
        return

    if act == "ram":
        cur = int(b.get("ram_limit_mb") or 0)
        pr = RAM_LIMIT_PRESETS
        nxt = pr[(pr.index(cur) + 1) % len(pr)] if cur in pr else pr[1]
        ctx.show(screen_env(store.update(bid, ram_limit_mb=nxt)))
        return

    if act == "schedset":
        set_flow(uid, "await_sched", bot_id=bid)
        ctx.edit(f"⏰ <b>Daily restart time</b>\nTime <code>HH:MM</code> ({SCHEDULE_TZ_LABEL}, 24h) me bhejo, jaise <code>04:30</code>.\n"
                 "Band karne ke liye <code>off</code> bhejo. /cancel to abort.",
                 KB([[("🔙 Cancel", cd("env", bid))]]))
        return

    if act == "schedoff":
        ctx.show(screen_env(store.update(bid, daily_restart=None)))
        return

    if act == "replace":
        set_flow(uid, "await_replace", bot_id=bid)
        ctx.edit(f"<b>📝 REPLACE FILE: {esc(b.get('name'))}</b>\n━━━━━━━━━━━━━━━━━━\n"
                 "Naya <code>.py</code> / <code>.js</code> / <code>.zip</code> Telegram document ki tarah bhejo.\n\n"
                 "• Running bot band hoga, file replace hogi, aur wo <b>auto-restart</b> hoga.\n"
                 "• Bot ID, name, settings wahi rahenge.\n"
                 "• Single .py/.js entry file replace karti hai (koi bhi file name chalega).\n"
                 "• Syntax error pehle pakda jata hai: purana bot chalta rehta hai.\n\n/cancel to abort.",
                 KB([[("🔙 Cancel", cd("cancel_flow", bid))]]))
        return

    if act == "cancel_flow":
        clear_flow(uid)
        ctx.show(screen_bot(b))
        return

    if act == "srgo":
        pend = SMART.pop(uid, None)
        if not pend:
            ctx.ans("Expired - file dobara bhejo.", True)
            return
        ctx.ans("⏳ Replacing...")
        ctx.edit(f"⏳ <b>{esc(b.get('name'))} replace ho raha hai...</b>")
        raw = download_tg(pend["file_id"])
        ok, text, kb = do_replace(uid, bid, pend["file_name"], raw)
        ctx.edit(text, kb)
        return

    if act == "rollback":
        ctx.show(screen_rollback(b))
        return

    if act == "rbpick":
        ctx.ans("⏳ Rolling back...")
        res = restore_backup(bid, extra)
        if not res.get("ok"):
            ctx.ans(f"Rollback failed: {res.get('error')}"[:180], True)
            return
        if res.get("was_running"):
            st2 = start_bot(bid)
            if not st2.get("ok"):
                store.update(bid, last_error=st2.get("error", "start failed"), status="crashed")
        ctx.show(screen_bot(store.get(bid)))
        return

    if act == "rename":
        set_flow(uid, "await_rename", bot_id=bid)
        ctx.edit(f"✏️ <b>Rename {esc(b.get('name'))}</b>\nNaya naam bhejo. /cancel to abort.",
                 KB([[("🔙 Cancel", cd("cancel_flow", bid))]]))
        return

    if act == "download":
        if not bot_dir(bid).exists():
            ctx.ans("Files missing", True)
            return
        try:
            ctx.ans("📥 ZIP ban raha hai...")
            ctx.send_doc(zip_dir(bot_dir(bid)), f"{safe_name(b.get('name', 'bot'))}.zip")
        except Exception as e:
            ctx.ans(str(e)[:180], True)
        return

    if act == "clone":
        if len(store.by_owner(uid)) >= slot_limit(uid):
            ctx.ans("Bot slot limit poori", True)
            return
        ctx.ans("🧬 Cloning...")
        new_id = new_bot_id()
        src, dst = bot_dir(bid), bot_dir(new_id)
        if src.exists():
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*RUNTIME_IGNORE))
        nb = dict(b)
        nb.update({"_id": new_id, "owner": uid, "name": safe_name(str(b.get("name", "bot")) + "_copy"),
                   "status": "stopped", "auto_restart": False, "created": now(), "last_started": None,
                   "last_error": "", "backups": [], "tg_msg": None, "tg_file": None, "tg_hash": None, "env": dict(b.get("env") or {}), "daily_restart": None})
        store.save(nb)
        write_dotenv(dst, nb["env"])
        sync.mark_bot(new_id)
        ctx.show(screen_bot(store.get(new_id)))
        return

    if act == "delask":
        ctx.edit(f"<b>🗑️ {esc(b.get('name', 'bot'))} delete karein?</b>\n━━━━━━━━━━━━━━━━━━\n"
                 "Bot ki files, config aur backups hat jayenge. Undo nahi hoga.",
                 KB([[("✅ Haan, delete", cd("delete", bid))], [("🔙 Cancel", cd("view", bid))]]))
        return

    if act == "delete":
        stop_bot(bid)
        sync.delete_bot_msgs(b)
        shutil.rmtree(bot_dir(bid), ignore_errors=True)
        shutil.rmtree(BACKUPS_DIR / bid, ignore_errors=True)
        store.delete(bid)
        RESTARTS.pop(bid, None)
        ctx.ans("Deleted")
        ctx.edit(f"<b>🗑️ Bot delete ho gaya.</b>\n\nBots: {len(store.by_owner(uid))}/{slot_limit(uid)}",
                 KB([[("🔙 My Bots", cd("list"))]]))
        return


# ======================================================================
# Background maintenance: crash detection, auto-restart, alerts, RAM, schedule
# ======================================================================
def alert_crash(b, rc, extra=""):
    bid = b["_id"]
    if not ALERTS_ENABLED or b.get("alerts", True) is False:
        return
    if time.time() - LAST_ALERT.get(bid, 0) < ALERT_COOLDOWN:
        return
    LAST_ALERT[bid] = time.time()
    tail = read_log_tail(bid, max_lines=10)
    mods = missing_modules(read_log_tail(bid, max_lines=80), bot_dir(bid))
    rows = pip_rows(bid, mods) if mods else []
    rows.append([("📜 Logs", cd("logs", bid)), ("🤖 Open Bot", cd("view", bid))])
    notify(b["owner"], f"💥 <b>{esc(b.get('name'))} crashed</b>\nExit code: <code>{rc}</code>{extra}\n\n"
                       f"<pre>{fit_pre(tail[-1200:], 1500) or '(no output)'}</pre>", KB(rows))


def auto_rollback(b, backup_ms, rc):
    bid = b["_id"]
    tail = read_log_tail(bid, max_lines=10)
    try:
        res = restore_backup(bid, backup_ms, make_new_backup=False)
        if not res.get("ok"):
            raise RuntimeError(res.get("error"))
        start = start_bot(bid)
        ok = bool(start.get("ok"))
        store.update(bid, status="running" if ok else "crashed",
                     last_error=(f"Naya version crash hua (exit {rc}); purana version wapas aa gaya." if ok
                                 else f"Auto-rollback hua par start fail: {start.get('error')}"))
        notify(b["owner"], f"⏪ <b>Auto-rollback: {esc(b.get('name'))}</b>\nNaya file {AUTO_ROLLBACK_SECONDS}s ke andar crash hua "
                           f"(exit <code>{rc}</code>), purana version restore kiya" + (" aur chal raha hai." if ok else ", par start nahi hua.")
                           + f"\n\n<pre>{fit_pre(tail[-1200:], 1500) or '(no output)'}</pre>",
               KB([[("📜 Logs", cd("logs", bid)), ("🤖 Open Bot", cd("view", bid))]]))
        return True
    except Exception:
        log.exception("auto-rollback failed for %s", bid)
        return False


def check_procs():
    for bid, info in list(PROCS.items()):
        if info_alive(info):
            trim_log(bid)
            continue
        if PROCS.get(bid) is not info:
            continue
        PROCS.pop(bid, None)
        po = info.get("popen")
        rc = po.returncode if po is not None else "unknown"
        append_log(bid, f"[process exited: {rc}]")
        b = store.get(bid)
        if not b:
            continue
        manual = bool(info.get("manual_stop"))
        within = (now() - int(info.get("started", 0))) <= AUTO_ROLLBACK_SECONDS
        if info.get("rollback_to") and not manual and within:
            if auto_rollback(b, info["rollback_to"], rc):
                continue
        if manual:
            store.update(bid, status="stopped")
            continue
        store.update(bid, status="crashed", last_error=f"Process exited with code {rc}")
        b = store.get(bid)
        try:
            alert_crash(b, rc, "\n🔁 Auto-restart ON hai." if b.get("auto_restart") else "")
        except Exception:
            log.exception("crash alert failed")
        if b.get("auto_restart"):
            if not restart_allowed(bid):
                store.update(bid, auto_restart=False,
                             last_error="Crash loop: 5 minute me 5 restarts ke baad auto-restart band. " + (b.get("last_error") or ""))
                if ALERTS_ENABLED and b.get("alerts", True) is not False:
                    notify(b["owner"], f"🚫 <b>{esc(b.get('name'))}</b> crash loop me hai - auto-restart band kar diya.")
                continue
            time.sleep(1)
            res = start_bot(bid)
            if not res.get("ok"):
                store.update(bid, last_error=res.get("error", "auto-restart failed"), status="crashed")


def ram_tick():
    for bid, info in list(PROCS.items()):
        b = store.get(bid)
        limit = int((b or {}).get("ram_limit_mb") or 0)
        if not b or not limit or not info_alive(info):
            continue
        rss = rss_of(info["pid"])
        if rss <= limit * 1024 * 1024:
            continue
        mb = rss // (1024 * 1024)
        append_log(bid, f"[host: RAM {mb} MB exceeded the {limit} MB limit - restarting]")
        if not restart_allowed(bid, max_restarts=5, window=900):
            stop_bot(bid)
            store.update(bid, ram_limit_mb=0)
            notify(b["owner"], f"🧠 <b>{esc(b.get('name'))}</b> baar baar {limit} MB cross kar raha hai. Stop kiya aur RAM limit OFF.")
            continue
        res = restart_bot(bid)
        notify(b["owner"], f"🧠 <b>{esc(b.get('name'))}</b> ne {mb} MB use kiya (limit {limit} MB) - "
               + ("restart ho gaya." if res.get("ok") else f"restart fail: {esc(res.get('error'))}"))


def schedule_tick():
    n = datetime.now(tz())
    hhmm, today = n.strftime("%H:%M"), n.strftime("%Y-%m-%d")
    for b in store.all():
        bid = b["_id"]
        if b.get("daily_restart") != hhmm or b.get("last_sched_day") == today:
            continue
        if not info_alive(PROCS.get(bid)):
            continue
        store.update(bid, last_sched_day=today)
        res = restart_bot(bid)
        notify(b["owner"], f"⏰ <b>{esc(b.get('name'))}</b> ka scheduled restart "
               + ("ho gaya." if res.get("ok") else f"fail: {esc(res.get('error'))}"))


def maintenance_loop():
    tick = 0
    while True:
        tick += 1
        for fn, every in ((check_procs, 1), (ram_tick, 3), (schedule_tick, 3)):
            if tick % every == 0:
                try:
                    fn()
                except Exception:
                    log.exception("maintenance %s failed", fn.__name__)
        time.sleep(5)


def boot_restore():
    """Host restart ke baad: running bots re-attach karo, jo running the unhe start karo."""
    resumed = []
    for b in store.all():
        bid = b["_id"]
        try:
            root = bot_dir(bid)
            root.mkdir(parents=True, exist_ok=True)
            kind, entry = entry_for_dir(root)
            if kind:
                store.update(bid, kind=kind, entry=entry)
            write_dotenv(root, b.get("env") or {})
            if kind and try_adopt(bid, entry, kind):
                store.update(bid, status="running", last_error="")
                continue
            if AUTO_START_ON_BOOT and b.get("status") == "running":
                res = start_bot(bid)
                if res.get("ok"):
                    resumed.append(b.get("name", bid))
                else:
                    store.update(bid, status="crashed", last_error=res.get("error", "startup failed"))
            elif b.get("status") == "running":
                store.update(bid, status="stopped")
        except Exception as e:
            log.exception("boot restore failed for %s", bid)
            store.update(bid, status="crashed", last_error=str(e))
    return resumed


# ======================================================================
# OWNER ALERTS  -  kuch bhi gadbad ho to owner ko Telegram par REASON ke saath batao
#   * button / command / background thread / polling / crash  -> sab pakde jate hain
#   * reason: BOT CODE  /  TELEGRAM  /  HOSTING SERVER  /  CONFIG  me se kaun sa hai
#   * same error baar-baar aaye to spam nahi hota.  /errors = history,  /diag = health check
# ======================================================================
ERROR_LOG = deque(maxlen=30)       # /errors ke liye last problems
_ALERT_Q = queue.Queue()
_ALERT_LOCK = threading.Lock()
_ALERT_SEEN = {}                   # signature -> [last_sent_ts, suppressed_count]
_ALERT_SENT = deque(maxlen=50)     # recent send times (rate-limit)
_ALERT_TLS = threading.local()
_ALERT_COUNTER = [0]

_NET_NAMES = {
    "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "SSLError", "ProxyError",
    "ChunkedEncodingError", "ConnectionResetError", "ConnectionAbortedError", "ConnectionRefusedError",
    "TimeoutError", "RemoteDisconnected", "ProtocolError", "MaxRetryError", "NewConnectionError",
}
_NET_WORDS = (
    "max retries exceeded", "name resolution", "temporary failure in name", "connection reset",
    "connection aborted", "remote end closed", "read timed out", "connect timeout",
    "network is unreachable", "connection refused", "eof occurred", "timed out",
)
_CODE_ERRORS = (NameError, AttributeError, TypeError, KeyError, IndexError, ValueError, UnboundLocalError,
                ZeroDivisionError, AssertionError, ImportError, RecursionError, SyntaxError, NotImplementedError)


def classify_problem(exc, msg=""):
    """Problem ka type batata hai: bot ke code ki galti hai, Telegram ki, ya hosting server ki.
    None = ignore (harmless)."""
    name = type(exc).__name__ if exc is not None else ""
    blob = (f"{name}: {exc}" if exc is not None else "") + " " + (msg or "")
    low = blob.strip().lower()

    code = getattr(exc, "error_code", None)
    if code is None:
        m = re.search(r"error code:\s*(\d{3})", low)
        code = int(m.group(1)) if m else None

    def r(title, who, hint, cooldown=None):
        return {"title": title, "who": who, "hint": hint, "cooldown": cooldown or OWNER_ALERT_COOLDOWN}

    # ---------------- Telegram API errors
    if code:
        if "message is not modified" in low:
            return None
        if code == 409 or "terminated by other getupdates" in low:
            return r("🔁 Isi token par DUSRA bot-instance bhi chal raha hai", "HOSTING / DEPLOY",
                     "Purana container/deploy ya doosra server band karo. Ek token par sirf ek hi instance chalna "
                     "chahiye, warna buttons aur commands kabhi chalenge kabhi nahi.", 900)
        if code == 401 or "unauthorized" in low:
            return r("🔑 Bot token galat ya revoke ho gaya", "CONFIG (BOT_TOKEN)",
                     "@BotFather se sahi/naya token lo, runbot.py me BOT_TOKEN me daalo aur redeploy karo.", 1800)
        if code == 403:
            if "blocked" in low or "deactivated" in low or "can't initiate" in low:
                return r("🚫 User ne bot block kiya / chat band hai", "TELEGRAM (user side)",
                         "Us user ne bot ko block ya stop kiya hai. Bot ki galti nahi hai.")
            return r("🚫 Telegram permission nahi de raha (403)", "TELEGRAM / CONFIG",
                     "Bot ko channel/chat me ADMIN banao (Post + Edit messages) ya check karo ki bot kicked to nahi.")
        if code == 429:
            return r("⏳ Telegram flood limit (bahut zyada requests)", "TELEGRAM (rate limit)",
                     "Bot thodi der ke liye slow hoga. Autosync/zyada bots start-stop karne se aisa hota hai. "
                     "AUTOSYNC_SECONDS badha do.", 600)
        if code >= 500:
            return r(f"☁️ Telegram server problem ({code})", "TELEGRAM (unki side)",
                     "Telegram ke servers me dikkat hai. Apne aap theek ho jata hai, kuch karna nahi.", 900)
        if code == 400:
            if "can't parse entities" in low or "unsupported start tag" in low:
                return r("🐞 Message ki HTML formatting galat", "BOT CODE",
                         "Kisi screen/text me galat HTML tag ya special character hai. Ye message mujhe bhej do, fix kar dunga.")
            if "button_data_invalid" in low or "button_url_invalid" in low:
                return r("🐞 Button ka data galat/lamba hai", "BOT CODE",
                         "Button callback_data 64 bytes se bada hai ya URL galat hai. Ye message bhej do, fix kar dunga.")
            if any(k in low for k in ("message to edit not found", "message can't be edited", "query is too old",
                                      "message to delete not found", "message identifier is not specified")):
                return r("🕰 Purane message/button par click hua", "TELEGRAM (message purana)",
                         "Panel purana ho gaya ya bot kuch der band tha. /hosting bhejke NAYA panel kholo, "
                         "purane buttons mat dabao.", 300)
            if "chat not found" in low or "channel_invalid" in low or "peer_id_invalid" in low:
                return r("📡 Chat/Channel nahi mila", "CONFIG (CHANNEL_ID)",
                         "CHANNEL_ID galat hai (-100... se shuru hona chahiye) ya bot us channel me hai hi nahi. "
                         "Channel me bot ko ADMIN banao.", 900)
            if "file is too big" in low or "too large" in low:
                return r("📦 File bahut badi hai", "TELEGRAM (20 MB limit)",
                         "Telegram bots 20 MB se badi file download nahi kar sakte. Chhoti zip/Git repo use karo.")
            return r(f"⚠️ Telegram ne request reject ki ({code})", "BOT CODE / DATA",
                     "Bot ne Telegram ko galat request bheji. Neeche ka error mujhe bhej do, fix kar dunga.")
        return r(f"⚠️ Telegram error ({code})", "TELEGRAM", "Error text padho, zyadatar Telegram/permission se juda hota hai.")

    # ---------------- network / server connectivity
    if name in _NET_NAMES or any(k in low for k in _NET_WORDS):
        return r("🌐 Server ko internet / Telegram se connection nahi mil raha", "HOSTING SERVER (network)",
                 "Hosting server ka internet ya DNS atka hai. Zyadatar apne aap theek ho jata hai. "
                 "Baar-baar aaye to hosting provider ka status dekho ya server restart karo.", 900)

    # ---------------- server resources
    if getattr(exc, "errno", None) == 28 or "no space left" in low:
        return r("💾 Server ki DISK full ho gayi", "HOSTING SERVER (disk)",
                 "Hosting plan ki storage bhar gayi. Purane bots/backups delete karo ya plan badhao.", 1800)
    if isinstance(exc, MemoryError) or "cannot allocate memory" in low or "out of memory" in low:
        return r("🧠 Server ki RAM khatam", "HOSTING SERVER (RAM)",
                 "RAM limit poori ho gayi. Kuch bots stop karo, RAM limit lagao ya plan upgrade karo.", 900)
    if isinstance(exc, PermissionError):
        return r("🔒 Server par file/folder permission nahi", "HOSTING SERVER (permission)",
                 "Bot ko is folder me likhne ki permission nahi. HOSTING_DATA_DIR kisi writable path par rakho.")
    if isinstance(exc, (subprocess.CalledProcessError, subprocess.TimeoutExpired)) or (
            ("pip" in low or "npm" in low) and ("install" in low or "error" in low)):
        return r("📦 Package install / command fail (pip/npm/git)", "HOSTING SERVER / HOSTED BOT",
                 "Hosted bot ki requirements ya server par pip/npm/git ki dikkat hai. Error text me package ka naam dekho.")
    if isinstance(exc, ModuleNotFoundError):
        return r("📦 Python package install nahi hai", "HOSTING SERVER (module missing)",
                 "Server par ye package nahi hai. `pip install <naam>` karo ya requirements.txt me daalo.")
    if isinstance(exc, FileNotFoundError):
        return r("📁 File/folder nahi mila", "HOSTING SERVER (disk) ya BOT CODE",
                 "Redeploy me disk wipe hui ho sakti hai (data channel se restore hota hai) ya path galat hai.")
    if name == "JSONDecodeError":
        return r("🗂 Database/config JSON kharab hai", "HOSTING SERVER (data file)",
                 "hosted_data ki JSON file adhoori save hui. Channel se restore hoga, ya file delete karke restart karo.")

    # ---------------- bot code
    if isinstance(exc, _CODE_ERRORS):
        return r("🐞 Bot ke CODE me error (bug)", "BOT CODE",
                 "runbot.py me bug hai. Ye poora message (traceback ke saath) mujhe bhej do, fix kar dunga.")

    return r("❓ Unknown error", "UNKNOWN",
             "Error text aur code location dekho. Samajh na aaye to ye message mujhe bhej do.")


def _code_location(exc):
    """runbot.py ki kaun si line/function par error aaya."""
    tb = getattr(exc, "__traceback__", None)
    if tb is None:
        return None
    try:
        frames = traceback.extract_tb(tb)
        here = os.path.abspath(__file__)
        mine = [f for f in frames if os.path.abspath(f.filename) == here]
        f = (mine or frames)[-1]
        return f"{os.path.basename(f.filename)}:{f.lineno} in {f.name}()"
    except Exception:
        return None


def _build_alert(rec, info, exc, suppressed):
    lines = [
        f"🚨 <b>BOT PROBLEM #{rec['n']}</b>  <i>{esc(fmt_ts(rec['ts']))} {esc(SCHEDULE_TZ_LABEL)}</i>",
        "━━━━━━━━━━━━━━━━━━",
        f"🔎 <b>Kya hua:</b> {esc(info['title'])}",
        f"🏷 <b>Problem kahan se:</b> {esc(info['who'])}",
        f"📍 <b>Kahan:</b> {esc(rec['where'][:200])}",
        f"❗ <b>Error:</b> <code>{esc(rec['err'])}</code>",
    ]
    loc = _code_location(exc)
    if loc:
        lines.append(f"📄 <b>Code location:</b> <code>{esc(loc)}</code>")
    lines.append(f"💡 <b>Matlab / kya karein:</b> {esc(info['hint'])}")
    if suppressed:
        lines.append(f"🔁 Ye problem pehle bhi {suppressed} baar aayi (spam rokne ke liye alert nahi bheja).")
    tb = getattr(exc, "__traceback__", None)
    if tb is not None and info["who"] in ("BOT CODE", "BOT CODE / DATA", "UNKNOWN"):
        tb_txt = "".join(traceback.format_exception(type(exc), exc, tb))[-900:]
        lines.append(f"\n<pre>{esc(tb_txt)}</pre>")
    return "\n".join(lines)[:3900]


def _send_alert_now(text):
    """True = ho gaya (ya retry bekaar hai). False = baad me dobara try karo."""
    try:
        bot.send_message(int(OWNER_ID), text, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception as e:
        code = getattr(e, "error_code", None)
        if code == 400:                       # HTML toot gaya -> plain text me bhejo
            try:
                bot.send_message(int(OWNER_ID), re.sub(r"<[^>]+>", "", html.unescape(text))[:4000])
            except Exception:
                pass
            return True
        if code in (401, 403):                # token galat / owner ne bot start nahi kiya - alert nahi ja sakta
            return True
        return False


def _alert_worker():
    _ALERT_TLS.busy = True                    # is thread ke logs se naye alerts na bane
    pending = deque(maxlen=20)
    while True:
        try:
            pending.append(_ALERT_Q.get(timeout=30))
        except queue.Empty:
            pass
        while pending:
            if _send_alert_now(pending[0]):
                pending.popleft()
                time.sleep(0.6)
            else:
                break                         # network down - 30s baad dobara


def report_problem(where, exc=None, msg="", sync=False):
    """Owner ko problem + reason bhejta hai. Ye function khud kabhi exception nahi uthata."""
    if getattr(_ALERT_TLS, "busy", False):
        return
    _ALERT_TLS.busy = True
    try:
        if not OWNER_ERROR_ALERTS or isinstance(exc, (KeyboardInterrupt, SystemExit)):
            return
        info = classify_problem(exc, msg)
        if info is None:
            return
        err = f"{type(exc).__name__}: {exc}" if exc is not None else (msg or "unknown")
        err = re.sub(r"\s+", " ", err)[:300]
        sig = (info["title"], re.sub(r"\d+", "N", err)[:100])
        t = time.time()
        with _ALERT_LOCK:
            _ALERT_COUNTER[0] += 1
            rec = {"n": _ALERT_COUNTER[0], "ts": int(t), "title": info["title"], "who": info["who"],
                   "err": err, "where": where}
            ERROR_LOG.append(rec)
            if len(_ALERT_SEEN) > 200:
                _ALERT_SEEN.clear()
            seen = _ALERT_SEEN.get(sig)
            if seen and t - seen[0] < info["cooldown"]:
                seen[1] += 1
                return
            recent = [x for x in _ALERT_SENT if t - x < 60]
            if len(recent) >= OWNER_ALERT_MAX_PER_MIN:
                _ALERT_SEEN.setdefault(sig, [0, 0])[1] += 1
                return
            suppressed = seen[1] if seen else 0
            _ALERT_SEEN[sig] = [t, 0]
            _ALERT_SENT.append(t)
        text = _build_alert(rec, info, exc, suppressed)
        if sync:
            _send_alert_now(text)
        else:
            _ALERT_Q.put(text)
    except Exception:
        pass
    finally:
        _ALERT_TLS.busy = False


class OwnerAlertHandler(logging.Handler):
    """Code me jahan bhi log.exception()/log.error() hota hai wo owner ko bhi chala jata hai."""

    def emit(self, record):
        try:
            if record.levelno < logging.ERROR:
                return
            exc = record.exc_info[1] if record.exc_info else None
            msg = record.getMessage()
            where = f"{record.name}: {msg}" if exc is not None else f"logger '{record.name}'"
            report_problem(where, exc, msg=msg)
        except Exception:
            pass


def _describe_update(obj, handler):
    try:
        uid = getattr(getattr(obj, "from_user", None), "id", "?")
        if hasattr(obj, "data"):
            return f"button '{obj.data}' (user {uid}, handler {handler})"
        ct = getattr(obj, "content_type", "message")
        txt = (getattr(obj, "text", None) or "")[:40]
        return f"{ct} message '{txt}' (user {uid}, handler {handler})"
    except Exception:
        return f"handler {handler}"


def _guard(fn):
    """Har command/button handler ko surakshit karo: error aaye to owner ko reason jaye."""
    @functools.wraps(fn)
    def inner(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            obj = a[0] if a else None
            report_problem(_describe_update(obj, getattr(fn, "__name__", "handler")), e)
            try:
                if obj is not None and not hasattr(obj, "data") and getattr(obj, "chat", None) is not None:
                    bot.send_message(obj.chat.id, "❌ Kuch gadbad ho gayi. Owner ko reason ke saath inform kar diya gaya hai.")
            except Exception:
                pass
    return inner


def install_alerting():
    root = logging.getLogger()
    if not any(isinstance(h, OwnerAlertHandler) for h in root.handlers):
        root.addHandler(OwnerAlertHandler(level=logging.ERROR))

    for dec_name in ("message_handler", "callback_query_handler"):
        def make(orig):
            def deco(*a, **kw):
                inner = orig(*a, **kw)
                return lambda fn: inner(_guard(fn))
            return deco
        setattr(bot, dec_name, make(getattr(bot, dec_name)))

    def _thread_hook(args):
        if args.exc_type is not None and issubclass(args.exc_type, (SystemExit, KeyboardInterrupt)):
            return
        tname = args.thread.name if args.thread else "?"
        report_problem(f"background thread '{tname}' crash ho gaya", args.exc_value)

    threading.excepthook = _thread_hook
    prev = sys.excepthook

    def _main_hook(t, v, tb):
        try:
            if v is not None and not issubclass(t, (KeyboardInterrupt, SystemExit)):
                report_problem("bot process CRASH - program band ho gaya", v, sync=True)
        finally:
            prev(t, v, tb)

    sys.excepthook = _main_hook
    threading.Thread(target=_alert_worker, daemon=True, name="owner-alerts").start()


# ======================================================================
# Base packages  -  runbot.py ke saath wali requirements.txt server par install karo
#   (hosted bots isi Python se chalte hain, isliye common packages pehle se mil jate hain)
# ======================================================================
def _pip_cmd(flags):
    return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-q", *flags]


def _base_requirements():
    out = []
    try:
        for ln in BASE_REQ_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            ln = ln.split("#", 1)[0].strip()
            if ln and not ln.startswith("-"):
                out.append(ln)
    except Exception:
        pass
    return out


def install_base_requirements():
    """Return: install NA hue packages ki list (khali list = sab theek).
    Pehle sab ek saath (already installed ho to turant ho jata hai); fail ho to ek-ek karke,
    taaki ek kharab package baaki sabko na rok de."""
    if not (AUTO_INSTALL_BASE_REQ and BASE_REQ_FILE.exists()):
        return []
    pkgs = _base_requirements()
    if not pkgs:
        return []
    deadline = time.time() + BASE_REQ_TIMEOUT
    flags = []
    for attempt in range(3):
        try:
            r = subprocess.run(_pip_cmd(flags) + ["-r", str(BASE_REQ_FILE)], capture_output=True, text=True,
                               timeout=BASE_REQ_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning("requirements.txt bulk install timeout (%ss)", BASE_REQ_TIMEOUT)
            break
        except Exception as e:
            log.warning("requirements.txt bulk install error: %s", e)
            break
        if r.returncode == 0:
            return []
        err = (r.stderr or r.stdout or "")
        low = err.lower()
        if "externally-managed" in low and "--break-system-packages" not in flags:
            flags = flags + ["--break-system-packages"]
            continue
        if ("permission denied" in low or "not writeable" in low or "errno 13" in low) and "--user" not in flags:
            flags = flags + ["--user"]      # server par global install ki permission nahi
            continue
        log.warning("requirements.txt bulk install failed, ek-ek karke try: %s", err[-300:])
        break
    failed = []
    for p in pkgs:
        left = deadline - time.time()
        if left <= 5:
            failed.append(p)
            continue
        try:
            r = subprocess.run(_pip_cmd(flags) + [p], capture_output=True, text=True, timeout=min(300, left))
            if r.returncode != 0:
                failed.append(p)
        except Exception:
            failed.append(p)
    return failed


# ======================================================================
# Entry point
# ======================================================================
def init():
    """Create folders, DB and the TeleBot instance (also used by tests)."""
    global store, bot, sync
    for d in (DATA_DIR, BOTS_DIR, BACKUPS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    store = Store(DB_FILE)
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, threaded=True, num_threads=8)
    install_alerting()
    register_handlers()
    sync = TgSync()


def main():
    if "PASTE_YOUR_BOT_TOKEN" in BOT_TOKEN or not OWNER_ID:
        raise SystemExit(
            "\nBOT_TOKEN aur OWNER_ID set nahi hain.\n"
            "  -> file ke upar CONFIG section me bharo, ya:\n"
            "     BOT_TOKEN=123:ABC OWNER_ID=123456789 CHANNEL_ID=-1001234567890 python hosting_bot.py\n"
            "  (BOT_TOKEN @BotFather se, OWNER_ID @userinfobot se milta hai)\n")
    init()
    try:
        bot.remove_webhook()
    except Exception:
        pass

    restored, notice = "", ""
    from_cfg = bool(CHANNEL_ID)
    if not from_cfg:
        saved = find_saved_channel()          # config.json ya owner chat ka pinned config
        if saved:
            set_channel_global(saved)
    if sync.enabled:
        err = sync.check()
        if err and from_cfg:
            raise SystemExit("\n" + err + "\n")
        if err:
            log.warning("saved channel kaam nahi kar raha: %s", err)
            notice = f"⚠️ Saved storage channel access nahi ho raha ({esc(err.splitlines()[0])}). /hosting → ☁️ Storage Channel se fix karo."
            set_channel_global(0)
    if sync.enabled:
        try:
            n_db, n_files = sync.restore()
            if n_db or n_files:
                restored = f"♻️ Channel se restore: DB {n_db} bot(s), files {n_files} bot(s)"
        except Exception:
            log.exception("channel restore failed")
    else:
        log.warning("Storage channel set nahi - data sirf local disk par hai (redeploy par ud sakta hai)")
        notice = notice or "⚠️ <b>Storage channel set nahi.</b> /hosting → ☁️ Storage Channel se set karo, warna redeploy par data ud sakta hai."

    req_note = ""
    if AUTO_INSTALL_BASE_REQ and BASE_REQ_FILE.exists():
        log.info("requirements.txt ke packages check/install ho rahe hain ...")
        t_req = time.time()
        _tm = threading.Timer(20, notify, args=(
            OWNER_ID, "📦 requirements.txt ke packages install ho rahe hain (pehli baar 2-5 min lag sakte hain). "
                      "Tab tak bot jawab nahi dega, thoda ruko - install ke baad khud online aa jayega."))
        _tm.daemon = True
        _tm.start()
        failed_pkgs = install_base_requirements()
        _tm.cancel()
        took = int(time.time() - t_req)
        if failed_pkgs:
            req_note = ("⚠️ requirements.txt ke ye packages install nahi hue: "
                        + ", ".join(esc(x) for x in failed_pkgs[:12]) + " (baaki ho gaye)")
            report_problem("startup: requirements.txt install",
                           RuntimeError("pip install failed: " + ", ".join(failed_pkgs[:10])))
        elif took > 15:
            req_note = f"📦 requirements.txt ke packages install ho gaye ({took}s)"

    resumed = boot_restore()
    if sync.enabled:
        for b in store.all():       # restore ke baad sab kuch channel me up-to-date rakho
            sync.mark_bot(b["_id"])
        store.dirty.set()
        sync.ensure_thread()
    threading.Thread(target=maintenance_loop, daemon=True, name="maintenance").start()

    import signal

    def _on_term(_sig, _frm):
        log.warning("SIGTERM - final channel sync")
        try:
            _t = threading.Thread(target=_send_alert_now, daemon=True, args=(
                "⏹ <b>Hosting bot band ho raha hai (SIGTERM)</b>\nServer/platform ne container stop kiya - "
                "redeploy, restart ya hosting plan/resource limit ki wajah se. Redeploy kiya tha to normal hai.",))
            _t.start()
            _t.join(3)
        except Exception:
            pass
        try:
            sync.flush_all()
        finally:
            os._exit(0)

    try:
        signal.signal(signal.SIGTERM, _on_term)
    except Exception:
        pass

    try:
        bot.set_my_commands([
            types.BotCommand("hosting", "Open hosting panel"),
            types.BotCommand("cancel", "Cancel current action"),
            types.BotCommand("start", "Start"),
            types.BotCommand("diag", "Health check (owner)"),
            types.BotCommand("errors", "Last problems (owner)"),
        ])
    except Exception:
        pass
    msg = f"✅ <b>Hosting bot online</b> (v{VERSION})"
    if OWNER_ERROR_ALERTS:
        msg += "\n🛡 Error alerts ON - koi problem aayi to reason ke saath yahin bataunga (/diag, /errors)"
    if restored:
        msg += "\n" + restored
    if notice:
        msg += "\n" + notice
    if req_note:
        msg += "\n" + req_note
    if resumed:
        msg += f"\n▶️ Auto-started {len(resumed)} bot(s):\n" + "\n".join(f"• {esc(n)}" for n in resumed[:15])
    notify(OWNER_ID, msg + "\n\n/hosting bhejo panel kholne ke liye.")
    log.info("Hosting bot running. Data dir: %s | channel sync: %s", DATA_DIR, "ON" if sync.enabled else "OFF")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30,
                         allowed_updates=["message", "edited_message", "channel_post", "callback_query", "my_chat_member"])


if __name__ == "__main__":
    main()