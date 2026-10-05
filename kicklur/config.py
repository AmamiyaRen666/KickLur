"""KickLur — configuration from the environment.

Protocol constants come from brutetolslhoya and are NOT changed: CLI version
2.1.97.1232.1, channel and_usa, login.ml.youngjoygame.com:30021.

Everything is settable through env vars so the Railway service can be tuned
without touching code.
"""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def load_dotenv(path=".env"):
    """Minimal .env loader; real environment always wins."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        return True
    except FileNotFoundError:
        return False


load_dotenv()

# ── Telegram ───────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
OWNER_CHAT_ID = (os.environ.get("TG_OWNER_CHAT_ID", "")
                 or os.environ.get("TG_CHAT_ID", "")).strip()
_allowed = os.environ.get("TG_ALLOWED_IDS", "").strip()
ALLOWED_IDS = {x.strip() for x in _allowed.split(",") if x.strip()}
if OWNER_CHAT_ID:
    ALLOWED_IDS.add(OWNER_CHAT_ID)

# ── MLBB protocol (from brutetolslhoya — unchanged) ────────────────────────
HOST = os.environ.get("MLBB_LOGIN_HOST", "login.ml.youngjoygame.com")
PORT = _int("MLBB_LOGIN_PORT", 30021)
VER = os.environ.get("MLBB_CLI_VER", "2.1.97.1232.1")
CHAN = os.environ.get("MLBB_CHANNEL", "and_usa")
LANG = os.environ.get("MLBB_LANG", "en")

# ── Kicker budget ──────────────────────────────────────────────────────────
# Each kick holds one thread for its socket timeout, so workers are I/O bound.
BF_WORKERS = max(1, _int("BF_WORKERS", 20))                  # per run
BF_MAX_CONCURRENCY = max(1, _int("BF_MAX_CONCURRENCY", 40))  # all runs
BF_MAX_DEVICES = max(1, _int("BF_MAX_DEVICES", 2000))        # per chat

# Pause between kicks, per worker. brutetolslhoya had none; a real client does
# not fire back-to-back either, and firing with no gap is what makes the game
# server answer "GAME SERVER REFUSED".
BF_KICK_DELAY = _float("BF_KICK_DELAY", 0.5)

# Resolve the nickname while fetching the profile (one 11153 per device).
BF_LOOKUP = (os.environ.get("BF_LOOKUP", "1").strip().lower()
             not in ("0", "false", "no", "off"))

# ── Timeouts (source values kept where they existed) ───────────────────────
OPEN_TIMEOUT = _float("MLBB_OPEN_TIMEOUT", 5)      # source Conn.open: 5s
KICK_TIMEOUT = _float("MLBB_KICK_TIMEOUT", 4.5)    # source Brute.kick: 4.5s
FETCH_ATTEMPTS = max(1, _int("MLBB_FETCH_ATTEMPTS", 3))  # source range(3)

# ── Telegram pacing ────────────────────────────────────────────────────────
# Global: minimum gap between ANY two outgoing calls.
TG_MIN_INTERVAL = _float("TG_MIN_INTERVAL", 0.12)
# Per chat: minimum gap between two calls to the SAME chat. Telegram limits per
# chat (~1 msg/s sustained, ~20/min in practice) and every run in that chat
# shares the budget, so this is the value that actually prevents a 429.
# 3.0s => at most 20 calls/minute per chat, which is the documented ceiling.
TG_CHAT_MIN_INTERVAL = _float("TG_CHAT_MIN_INTERVAL", 3.0)
# How often the run panel refreshes itself while it is on screen. The panel is
# only refreshed while it still shows Status, so this is the "live" tick rate.
PANEL_REFRESH_SEC = _float("PANEL_REFRESH_SEC", 15.0)
PORT_HTTP = _int("PORT", 0)


def validate():
    problems = []
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        problems.append("TG_BOT_TOKEN is missing or malformed")
    if not OWNER_CHAT_ID:
        problems.append("TG_OWNER_CHAT_ID (or TG_CHAT_ID) is missing")
    return problems
