"""Sumire — configuration from the environment.

Only the BF Kicker needs configuration: a bot token, an owner chat, and the two
concurrency knobs. Everything else has a working default.
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

# ── Telegram ────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
OWNER_CHAT_ID = (os.environ.get("TG_OWNER_CHAT_ID", "")
                 or os.environ.get("TG_CHAT_ID", "")).strip()

# ── Worker / concurrency budgets ────────────────────────────────────────────
# One kick is ~0.017 ms of CPU (packet build + zstd) and a few network round
# trips, so threads are waiting on sockets, not burning the core.
#
# Measured against the live game server (2026-10, 10 servers per kick):
#
#   workers  kick/s  failures
#        20    19.9   0.0%
#        30    27.5   0.0%
#        40    37.2   0.0%   <- throughput keeps climbing
#        55    38.5   0.0%   <- plateau
#        60    93.1  68.9%   <- collapses: the game server starts refusing
#
# So 20 is a safe default and up to ~40 is free. Past ~50 the server refuses
# and the extra workers turn into pure failure - do not go there.
BF_WORKERS = max(1, _int("BF_WORKERS", 20))               # per run
BF_MAX_CONCURRENCY = max(1, _int("BF_MAX_CONCURRENCY", 40))  # across all runs
BF_MAX_DEVICES = max(1, _int("BF_MAX_DEVICES", 2000))     # per chat list

# Pause between kicks, per worker. The original BF Kicker sleeps BF_KICK_DELAY
# (0.5s) after every kick plus a 0.01s login rate limit; that pacing is what
# keeps the login/game server from treating the device as a bot. Sending with no
# gap at all produced "Login server rejected the device id" and
# "GAME SERVER REFUSED" in testing.
BF_KICK_DELAY = _float("BF_KICK_DELAY", 0.5)

# ── Game protocol ───────────────────────────────────────────────────────────
KICK_CLI_VERSION = os.environ.get("MLBB_CLI_VER", "2.2.16.1232.1")
KICK_CHANNEL = os.environ.get("MLBB_CHANNEL", "and_usa")
KICK_HOST = os.environ.get("MLBB_LOGIN_HOST", "login.ml.youngjoygame.com")
KICK_PORT = _int("MLBB_LOGIN_PORT", 30021)

# Resolve the account nickname for the status list. It rides on the same
# game-server connection as the kick (packet 11153), so it costs a couple of
# extra round trips per kick — and nothing at all once an account is cached.
BF_LOOKUP = (os.environ.get("BF_LOOKUP", "1").strip().lower()
             not in ("0", "false", "no", "off"))

PROFILE_TIMEOUT = _float("MLBB_PROFILE_TIMEOUT", 15)
PROFILE_ATTEMPTS = max(1, _int("MLBB_PROFILE_ATTEMPTS", 3))
PROFILE_BACKOFF = _float("MLBB_PROFILE_BACKOFF", 1.5)
KICK_TIMEOUT = _float("MLBB_KICK_TIMEOUT", 3)

# How long to wait for the game server to acknowledge the kick.
#
# Measured against the live server (2026-10):
#   TCP connect to the game server : ~189 ms
#   send the kick packet           : ~0.2 ms
#   server acknowledges            : ~570 ms LATER
#
# The server answers EVERY kick with packet 10002. Closing the socket before
# that answer arrives makes our kernel reply with an RST — not how a real client
# behaves, and the one concrete difference from the original BF Kicker, which
# holds the socket open until the ack. Waiting costs one kick's wall time and
# kicks run in parallel, so it is cheap next to actually landing the kick.
BF_ACK_WAIT = _float("BF_ACK_WAIT", 1.5)

# Kick every known game server instead of just the one the login handed us.
#
# Default OFF, matching the original BF Kicker. Measured against the live
# server: fanning out to the whole fleet multiplies the kick packets per device
# (10 servers + 1 lookup = 11 packets) and that is what trips the game server
# into "GAME SERVER REFUSED". One kick per device is what a real client sends
# and is the behaviour that works.
BF_KICK_ALL_SERVERS = (os.environ.get("BF_KICK_ALL_SERVERS", "0").strip().lower()
                       not in ("0", "false", "no", "off"))

# Send the kick twice. The packet write is ~0.2 ms, so a second copy is free
# next to the ~189 ms connect, and it covers a dropped/late first frame.
BF_KICK_TWICE = (os.environ.get("BF_KICK_TWICE", "0").strip().lower()
                 not in ("0", "false", "no", "off"))

# Optional seed list, "host:port,host:port". Only used when
# BF_KICK_ALL_SERVERS=1 (fan-out mode); the normal path kicks the address the
# login returned, exactly like the original BF Kicker.
MLBB_GS_SEED = [a.strip() for a in
                os.environ.get("MLBB_GS_SEED", "").split(",") if a.strip()]

# ── Telegram pacing ─────────────────────────────────────────────────────────
# Outgoing calls only; the long poll is never gated. A run rewrites its message
# on demand and once at the end, so this is a safety net, not the main limiter.
TG_MIN_INTERVAL = _float("TG_MIN_INTERVAL", 0.12)
TG_MAX_DOC_MB = _float("TG_MAX_DOC_MB", 50)

PORT = _int("PORT", 0)


def validate():
    problems = []
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        problems.append("TG_BOT_TOKEN is missing or malformed")
    if not OWNER_CHAT_ID:
        problems.append("TG_OWNER_CHAT_ID (or TG_CHAT_ID) is missing")
    return problems
