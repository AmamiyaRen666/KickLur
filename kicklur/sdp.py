"""SDP wire format + TCP connection.

Copied verbatim from the working implementation. This is the on-the-wire
protocol the game servers speak, so the packing rules (including the tag-15
sentinel and the zstd frame header) must stay byte-for-byte identical.
"""
import socket
import struct
import zlib
from enum import Enum

import zstandard as zstd
from Crypto.Cipher import AES

AES_KEY = bytes.fromhex("f5a193d50ade553e9835595f5cd75ddd")
AES_IV = b"\x00" * 16


class SdpType(Enum):
    INT_POS = 0
    INT_NEG = 1
    FLOAT = 2
    DOUBLE = 3
    STRING = 4
    LIST = 5
    DICT = 6
    STRUCT_BEGIN = 7
    STRUCT_END = 8


class SdpStruct(dict):
    __slots__ = ("data", "offset")

    def __init__(self, data=None):
        super().__init__()
        self.data = b""
        self.offset = 0
        if isinstance(data, bytes):
            self.data = data
            self._unpack()
        elif data is not None:
            self.update(data)
            self._pack()

    def _pack(self):
        self.data = bytes([SdpType.STRUCT_BEGIN.value << 4])
        for k, v in sorted(self.items()):
            self._pack_one(k, v)
        self.data += bytes([SdpType.STRUCT_END.value << 4])

    def _unpack(self):
        if not self.data:
            return
        if self.data[0] >> 4 == SdpType.STRUCT_BEGIN.value:
            self.offset = 1
        while self.offset < len(self.data):
            k, v = self._unpack_one()
            if isinstance(v, SdpType) and v == SdpType.STRUCT_END:
                break
            self[k] = v

    def _w_num(self, n):
        r = bytearray()
        while n >= 0x80:
            r.append((n & 0x7F) | 0x80)
            n >>= 7
        r.append(n & 0x7F)
        return bytes(r)

    def _hdr(self, tag, dtype):
        """Write a type/tag header.

        Tags 0-14 fit in the low nibble. Tag 15+ is a sentinel: the low nibble
        becomes 0x0F and the real tag follows as a varint. Without this the tag
        is silently OR-ed into the nibble, so tag 15 (the device id in the
        session-kick packet) is dropped and the server never reads it.
        """
        if tag < 15:
            self.data += bytes([(dtype.value << 4) | tag])
        else:
            self.data += bytes([(dtype.value << 4) | 15]) + self._w_num(tag)

    def _r_num(self):
        n = 1
        v = self.data[self.offset] & 0x7F
        while (self.offset + n - 1 < len(self.data)
               and self.data[self.offset + n - 1] >= 0x80):
            if self.offset + n >= len(self.data):
                break
            v |= (self.data[self.offset + n] & 0x7F) << (7 * n)
            n += 1
            if n > 5:
                break  # prevent overflow
        self.offset += n
        return v

    def _pack_one(self, tag, val):
        if val is None:
            return
        if isinstance(val, bool):
            self._hdr(tag, SdpType.INT_POS)
            self.data += self._w_num(1 if val else 0)
        elif isinstance(val, int):
            if val < 0:
                self._hdr(tag, SdpType.INT_NEG)
                self.data += self._w_num(-val)
            else:
                self._hdr(tag, SdpType.INT_POS)
                self.data += self._w_num(val)
        elif isinstance(val, float):
            self._hdr(tag, SdpType.DOUBLE)
            self.data += self._w_num(8) + struct.pack("<d", val)
        elif isinstance(val, (str, bytes)):
            enc = val.encode() if isinstance(val, str) else val
            self._hdr(tag, SdpType.STRING)
            self.data += self._w_num(len(enc)) + enc
        elif isinstance(val, list):
            self._hdr(tag, SdpType.LIST)
            self.data += self._w_num(len(val))
            for it in val:
                self._pack_one(0, it)
        elif isinstance(val, dict):
            if isinstance(val, SdpStruct):
                self.data += bytes([(SdpType.STRUCT_BEGIN.value << 4) | tag])
                for k, v in sorted(val.items()):
                    self._pack_one(k, v)
                self.data += bytes([(SdpType.STRUCT_END.value << 4)])
            else:
                self._hdr(tag, SdpType.DICT)
                self.data += self._w_num(len(val))
                for k, v in sorted(val.items()):
                    self._pack_one(0, k)
                    self._pack_one(0, v)

    def _unpack_one(self):
        try:
            if self.offset >= len(self.data):
                return 0, None
            h = self.data[self.offset]
            tag = h & 0xF
            dt = SdpType(h >> 4)
            self.offset += 1
            if tag == 15:
                tag = self._r_num()
            if dt == SdpType.INT_POS:
                return tag, self._r_num()
            if dt == SdpType.INT_NEG:
                return tag, -self._r_num()
            if dt == SdpType.FLOAT:
                n = self._r_num()
                v = self.data[self.offset:self.offset + n].ljust(4, b"\x00")
                self.offset += n
                return tag, struct.unpack("<f", v)[0]
            if dt == SdpType.DOUBLE:
                n = self._r_num()
                v = self.data[self.offset:self.offset + n].ljust(8, b"\x00")
                self.offset += n
                return tag, struct.unpack("<d", v)[0]
            if dt == SdpType.STRING:
                ln = self._r_num()
                try:
                    v = self.data[self.offset:self.offset + ln].decode("utf-8")
                except Exception:
                    v = self.data[self.offset:self.offset + ln]
                self.offset += ln
                return tag, v
            if dt == SdpType.LIST:
                ln = self._r_num()
                return tag, [self._unpack_one()[1] for _ in range(ln)]
            if dt == SdpType.DICT:
                ln = self._r_num()
                v = {}
                for _ in range(ln):
                    _, k = self._unpack_one()
                    _, val = self._unpack_one()
                    v[k] = val
                return tag, v
            if dt == SdpType.STRUCT_BEGIN:
                d = {}
                while True:
                    st, sv = self._unpack_one()
                    if isinstance(sv, SdpType) and sv == SdpType.STRUCT_END:
                        break
                    d[st] = sv
                return tag, SdpStruct(d)
            if dt == SdpType.STRUCT_END:
                return tag, SdpType.STRUCT_END
        except Exception:
            pass
        return 0, None


class BaseConn:
    __slots__ = ("host", "port", "sequence", "socket", "_rxbuf")

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sequence = 1
        self.socket = None
        self._rxbuf = b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.cleanup()
        return False

    def connect(self, host=None, port=None, timeout=6):
        if host:
            self.host = host
        if port:
            self.port = port
        self.sequence = 1
        # A reused connection must not carry leftover bytes of the previous
        # session into the first frame of the new one.
        self._rxbuf = b""
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.socket.connect((self.host, self.port))
        self.socket.settimeout(timeout)

    def cleanup(self):
        self._rxbuf = b""
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.sequence = 1
            self.socket = None

    def send_data(self, pkt_id, sdp):
        pkt = SdpStruct({0: pkt_id, 1: self.sequence, 5: sdp.data}).data
        buf = zstd.compress(pkt)
        flags = (len(buf) + 4) | (16 << 24)
        self.socket.sendall(flags.to_bytes(4, "big") + buf)
        self.sequence += 1

    def recv_data(self):
        # The TCP stream can carry several SDP frames in one segment, so keep
        # the remainder buffered instead of discarding it.
        try:
            if getattr(self, "_rxbuf", None) is None:
                self._rxbuf = b""
            q = self._rxbuf
            self._rxbuf = b""
            while len(q) < 4:
                d = self.socket.recv(4096)
                if not d:
                    return None, None
                q += d
            flags = int.from_bytes(q[:4], "big")
            size = flags & 0xFFFFFF
            ctype = flags >> 24
            if size < 4 or size > 10_000_000:
                return None, None
            while len(q) < size:
                d = self.socket.recv(4096)
                if not d:
                    return None, None
                q += d
            data = q[4:size]
            self._rxbuf = q[size:]
            if ctype == 1:
                data = zlib.decompress(data)
            elif ctype == 16:
                data = zstd.decompress(data)
            elif ctype in (2, 3, 18):
                cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = cipher.decrypt(
                    data[:-1] if len(data) % 16 else data).rstrip(b"\x00")
                if ctype == 3:
                    data = zlib.decompress(data)
                elif ctype == 18:
                    data = zstd.decompress(data)
            res = SdpStruct(data)
            pid = res.get(0)
            if pid is None:
                return None, None
            body = res.get(6) or res.get(5)
            return pid, SdpStruct(body) if body and isinstance(body, bytes) else None
        except socket.timeout:
            return -1, None
        except socket.error:
            return None, None
        except Exception:
            return None, None
