"""Telegram client: long poll + paced sends, with 429 handling.

Deliberately minimal — no framework, so the Railway image stays small and the
bot has no hidden scheduler. Every outgoing call goes through one lock with a
minimum interval, so a run can never trip the per-chat flood limit.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config as cfg

API = "https://api.telegram.org"


class Telegram:
    def __init__(self, token):
        self.token = token
        self._url = f"{API}/bot{token}"
        self._lock = threading.Lock()
        self._last_send = 0.0
        self._offset = None
        self.flood_until = 0.0

    # ── low level ──────────────────────────────────────────────────────────
    def _call(self, method, params=None, files=None, timeout=30):
        params = params or {}
        if files:
            body, ctype = self._multipart(params, files)
            req = urllib.request.Request(
                f"{self._url}/{method}", data=body,
                headers={"Content-Type": ctype}, method="POST")
        else:
            req = urllib.request.Request(
                f"{self._url}/{method}",
                data=urllib.parse.urlencode(params).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode())
            except Exception:
                payload = {"ok": False, "description": f"HTTP {e.code}"}
            if e.code == 429:
                wait = (payload.get("parameters") or {}).get("retry_after", 5)
                self.flood_until = time.time() + float(wait)
            return payload
        except Exception as e:
            return {"ok": False, "description": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _multipart(params, files):
        boundary = "----KickLurBoundary7f3a"
        parts = []
        for k, v in params.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                         f'name="{k}"\r\n\r\n{v}\r\n'.encode())
        for k, (name, data) in files.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                         f'name="{k}"; filename="{name}"\r\n'
                         f"Content-Type: application/octet-stream\r\n\r\n".encode())
            parts.append(data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def _pace(self):
        with self._lock:
            now = time.time()
            if now < self.flood_until:
                time.sleep(self.flood_until - now)
                now = time.time()
            gap = now - self._last_send
            if gap < cfg.TG_MIN_INTERVAL:
                time.sleep(cfg.TG_MIN_INTERVAL - gap)
            self._last_send = time.time()

    # ── api ────────────────────────────────────────────────────────────────
    def me(self):
        return self._call("getMe")

    def send(self, chat_id, text, keyboard=None, parse_mode=None):
        self._pace()
        p = {"chat_id": chat_id, "text": text[:4096],
             "disable_web_page_preview": "true"}
        if parse_mode:
            p["parse_mode"] = parse_mode
        if keyboard is not None:
            p["reply_markup"] = json.dumps(keyboard)
        return self._call("sendMessage", p)

    def edit(self, chat_id, message_id, text, keyboard=None, parse_mode=None):
        self._pace()
        p = {"chat_id": chat_id, "message_id": message_id, "text": text[:4096],
             "disable_web_page_preview": "true"}
        if parse_mode:
            p["parse_mode"] = parse_mode
        if keyboard is not None:
            p["reply_markup"] = json.dumps(keyboard)
        r = self._call("editMessageText", p)
        if not r.get("ok") and "not modified" in str(r.get("description", "")):
            return {"ok": True, "unchanged": True}
        return r

    def answer_callback(self, callback_id, text=""):
        return self._call("answerCallbackQuery",
                          {"callback_query_id": callback_id, "text": text[:200]})

    def send_document(self, chat_id, filename, data, caption=""):
        self._pace()
        p = {"chat_id": chat_id}
        if caption:
            p["caption"] = caption[:1000]
        return self._call("sendDocument", p, files={"document": (filename, data)})

    def get_file(self, file_id):
        r = self._call("getFile", {"file_id": file_id})
        if not r.get("ok"):
            return None
        path = r["result"].get("file_path")
        if not path:
            return None
        url = f"{API}/file/bot{self.token}/{path}"
        try:
            with urllib.request.urlopen(url, timeout=60) as fh:
                return fh.read()
        except Exception:
            return None

    # ── polling ────────────────────────────────────────────────────────────
    def poll(self, timeout=25):
        p = {"timeout": timeout}
        if self._offset is not None:
            p["offset"] = self._offset
        r = self._call("getUpdates", p, timeout=timeout + 10)
        if not r.get("ok"):
            time.sleep(2)
            return []
        out = []
        for upd in r.get("result", []):
            self._offset = upd["update_id"] + 1
            out.append(upd)
        return out
