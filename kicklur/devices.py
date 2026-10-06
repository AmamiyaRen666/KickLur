"""Device-id parsing and the per-chat accumulating list.

The list accumulates on purpose: sending ids one at a time builds up a set
instead of the newest message replacing the previous one.
"""
import re
import threading

from . import config as cfg

DEVICE_RE = re.compile(r"(?i)(?:and_|ios_)[A-Za-z0-9_-]+")
DEVICE_RE_STRICT = re.compile(r"^(?:and_|ios_)[A-Za-z0-9_-]+$")


def extract_device_ids(text):
    ids, seen = [], set()
    for line in (text or "").split("\n"):
        for m in DEVICE_RE.finditer(line):
            d = m.group(0)
            if d not in seen:
                seen.add(d)
                ids.append(d)
    return ids


def normalise_token(tok):
    """Accept a prefixed device id, or a bare one that clearly looks like one.

    Real ids are and_<56 hex>-<uuid> or ios_<uuid>, so a bare token is only
    auto-prefixed when it is long and hex-ish. Anything else is left alone so
    ordinary words are reported as invalid instead of becoming "and_halo".
    """
    tok = (tok or "").strip().strip(",;|")
    if not tok:
        return None
    if tok.lower().startswith(("and_", "ios_")):
        return tok
    if len(tok) >= 32 and re.fullmatch(r"[0-9a-fA-F-]{32,}", tok):
        return "and_" + tok
    return None


def split_tokens(text):
    return [t for t in re.split(r"[\s,;|]+", (text or "").strip()) if t]


def parse_device_input(text):
    """Return (valid_ids, unparsed_tokens)."""
    found, seen, unparsed = [], set(), []
    for t in split_tokens(text):
        d = normalise_token(t)
        if d and DEVICE_RE_STRICT.match(d) and d not in seen:
            seen.add(d)
            found.append(d)
        else:
            unparsed.append(t)
    return found, unparsed


def looks_like_device_list(text):
    """True when every token is a bare device id (and_... / ios_...)."""
    toks = split_tokens(text)
    return bool(toks) and all(DEVICE_RE_STRICT.match(t) for t in toks)


def mentions_device_token(text):
    return bool(re.search(r"(?i)(?:and_|ios_)", text or ""))


class DeviceListStore:
    """Per-chat accumulating device list, deduped and capped."""

    def __init__(self, cap=None):
        self.cap = cap or cfg.BF_MAX_DEVICES
        self._lock = threading.Lock()
        self._data = {}

    def add(self, chat_id, ids):
        """Merge ids in. Returns (total, added)."""
        added = 0
        with self._lock:
            cur = self._data.setdefault(str(chat_id), [])
            seen = set(cur)
            for d in ids:
                if not d or d in seen:
                    continue
                if len(cur) >= self.cap:
                    break
                seen.add(d)
                cur.append(d)
                added += 1
            return len(cur), added

    def get(self, chat_id):
        with self._lock:
            return list(self._data.get(str(chat_id), []))

    def clear(self, chat_id):
        with self._lock:
            self._data.pop(str(chat_id), None)

    def count(self, chat_id):
        with self._lock:
            return len(self._data.get(str(chat_id), []))
