"""Minimal Telegram client: pacing, flood handling, long polling.

Only outgoing calls are paced. The long poll is a single blocked request, so
gating it would only add latency.
"""
import html
import threading
import time
from pathlib import Path

import requests

from . import config as cfg


class Telegram:
    def __init__(self, token):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()
        self._lock = threading.Lock()
        self._last = 0.0
        self._flood_until = 0.0

    # ── pacing ─────────────────────────────────────────────────────────────
    def flood_wait_left(self):
        with self._lock:
            return max(0.0, self._flood_until - time.time())

    def _gate(self):
        while True:
            with self._lock:
                now = time.time()
                if now < self._flood_until:
                    wait = self._flood_until - now
                elif now - self._last < cfg.TG_MIN_INTERVAL:
                    wait = cfg.TG_MIN_INTERVAL - (now - self._last)
                else:
                    self._last = now
                    return
            time.sleep(min(wait, 5.0))

    def api(self, method, **payload):
        """POST one API call. Returns the decoded body, or {'ok': False}."""
        if self.flood_wait_left() > 0:
            return {"ok": False, "flooded": True,
                    "retry_after": self.flood_wait_left()}
        self._gate()
        try:
            r = self.session.post(f"{self.base}/{method}", json=payload, timeout=25)
            data = r.json()
        except Exception:
            return {"ok": False}
        if r.status_code == 429:
            retry_after = (data.get("parameters") or {}).get("retry_after", 5)
            with self._lock:
                # Honour the full penalty: capping it just produces more 429s.
                self._flood_until = time.time() + retry_after
            return {"ok": False, "flooded": True, "retry_after": retry_after}
        return data

    # ── convenience wrappers ───────────────────────────────────────────────
    def send(self, chat_id, text, keyboard=None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        r = self.api("sendMessage", **payload)
        if r.get("ok"):
            return r["result"]["message_id"]
        return None

    def edit(self, chat_id, message_id, text, keyboard=None):
        payload = {"chat_id": chat_id, "message_id": message_id,
                   "text": text, "parse_mode": "HTML"}
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        elif keyboard is None:
            payload["reply_markup"] = {"inline_keyboard": []}
        r = self.api("editMessageText", **payload)
        return r.get("ok", False)

    def answer(self, callback_id, text="", alert=False):
        if callback_id is None:
            return False
        return self.api("answerCallbackQuery", callback_query_id=callback_id,
                        text=text, show_alert=alert).get("ok", False)

    def send_document(self, chat_id, filepath, caption=""):
        path = Path(filepath)
        if not path.exists():
            return False
        if path.stat().st_size / (1024 * 1024) > cfg.TG_MAX_DOC_MB:
            self.send(chat_id, f"⚠️ File terlalu besar: <code>{html.escape(path.name)}</code>")
            return False
        self._gate()
        try:
            with open(path, "rb") as fh:
                r = self.session.post(f"{self.base}/sendDocument",
                                      files={"document": fh},
                                      data={"chat_id": chat_id,
                                            "caption": caption[:1024]},
                                      timeout=120)
            return r.json().get("ok", False)
        except Exception:
            return False

    def download(self, file_id, dest_dir):
        info = self.api("getFile", file_id=file_id)
        if not info.get("ok"):
            return None
        try:
            remote = info["result"]["file_path"]
            url = f"https://api.telegram.org/file/bot{self.token}/{remote}"
            r = self.session.get(url, timeout=180)
            if r.status_code != 200:
                return None
            dest = Path(dest_dir) / f"up_{int(time.time())}_{Path(remote).name}"
            dest.write_bytes(r.content)
            return str(dest)
        except Exception:
            return None

    # ── polling ────────────────────────────────────────────────────────────
    def poll(self, offset=0, timeout=25):
        """Long-poll for updates. Returns list of updates."""
        r = self.get_updates(offset, timeout)
        return r.get("result", []) if r.get("ok") else []

    def get_updates(self, offset, timeout=25):
        try:
            r = self.session.get(f"{self.base}/getUpdates",
                                 params={"offset": offset, "timeout": timeout},
                                 timeout=timeout + 15)
            return r.json()
        except Exception:
            return {"ok": False}

    def me(self):
        return self.api("getMe")
