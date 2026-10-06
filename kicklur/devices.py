"""Device id parsing + the per-chat device list (accumulative).

Ported from brutetolslhoya's input handling: it accepts and_/ios_ ids, one per
line or embedded in a longer line, and dedupes.
"""
import re
import threading

from . import config as cfg

# and_<body> or ios_<body>; the source's pecah() only needs the prefix+body
_ID_RE = re.compile(r"(?:and|ios)_[A-Za-z0-9._-]{8,512}")


def extract_ids(text):
    """Pull every device id out of a blob of text, in order, deduped."""
    seen = []
    seen_set = set()
    for m in _ID_RE.finditer(text or ""):
        did = m.group(0)
        if did not in seen_set:
            seen_set.add(did)
            seen.append(did)
    return seen


def is_device_id(value):
    v = (value or "").strip()
    if not v.startswith(("and_", "ios_")):
        return False
    return bool(_ID_RE.fullmatch(v))


class DeviceList:
    """Accumulative per-chat list. Adding never replaces; Reset clears."""

    def __init__(self):
        self._by_chat = {}
        self._lock = threading.Lock()

    def add(self, chat_id, ids):
        """Returns (added, total, capped)."""
        added = 0
        with self._lock:
            lst = self._by_chat.setdefault(chat_id, [])
            have = set(lst)
            for did in ids:
                if did in have:
                    continue
                if len(lst) >= cfg.BF_MAX_DEVICES:
                    return added, len(lst), True
                lst.append(did)
                have.add(did)
                added += 1
            return added, len(lst), False

    def get(self, chat_id):
        with self._lock:
            return list(self._by_chat.get(chat_id, []))

    def reset(self, chat_id):
        with self._lock:
            n = len(self._by_chat.get(chat_id, []))
            self._by_chat[chat_id] = []
            return n
