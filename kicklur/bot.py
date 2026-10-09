"""Update dispatch: Telegram update -> action.

Owner-only. State per chat: the device list and the runs.
"""
import html
import math
import os
import threading
import time
from pathlib import Path

from . import config as cfg
from . import ui
from .devices import (DeviceListStore, extract_device_ids,
                      looks_like_device_list, mentions_device_token,
                      parse_device_input)
from .runs import RunExecutor, RunRegistry, run_finished_text, MENU_KB
from .telegram import Telegram

MENU = "menu"


class KickLurBot:
    def __init__(self, tg=None):
        self.tg = tg or Telegram(cfg.BOT_TOKEN)
        self.devices = DeviceListStore()
        self.registry = RunRegistry()
        self.offset = 0
        self._run_lock = threading.Lock()

    # ── helpers ────────────────────────────────────────────────────────────
    def _allowed(self, chat_id):
        allowed = {str(cfg.OWNER_CHAT_ID)}
        extra = {x.strip() for x in os.environ.get("TG_ALLOWED_IDS", "").split(",")
                 if x.strip()}
        allowed |= extra
        return str(chat_id) in allowed

    def _send_menu(self, chat_id, message_id=None):
        n = self.devices.count(chat_id)
        runs = self.registry.active(chat_id)
        text, kb = ui.main_menu(n, len(runs), runs=runs)
        if message_id:
            self.tg.edit(chat_id, message_id, text, kb)
        else:
            self.tg.send(chat_id, text, kb)

    def _show_run_status(self, run, message_id=None):
        active_runs = self.registry.active(run.chat_id)
        text, kb = ui.run_status(run, active_runs=active_runs)
        if message_id:
            self.tg.edit(run.chat_id, message_id, text, kb)
        else:
            self.tg.send(run.chat_id, text, kb)

    def _show_run_device_panel(self, run, message_id, page=0):
        active_runs = self.registry.active(run.chat_id)
        text, kb = ui.device_panel(run, page=page, active_runs=active_runs)
        self.tg.edit(run.chat_id, message_id, text, kb)

    # ── dispatch ───────────────────────────────────────────────────────────
    def handle(self, upd):
        if "message" in upd:
            self._on_message(upd["message"])
        elif "callback_query" in upd:
            self._on_callback(upd["callback_query"])

    def _on_message(self, msg):
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not self._allowed(chat_id):
            self._bg(self.tg.send, chat_id, "⛔ Bot ini owner-only.")
            return

        text = (msg.get("text") or "").strip()

        if msg.get("document"):
            self._bg(self._on_document, chat_id, msg["document"])
            return

        if text.startswith("/"):
            cmd = text.split()[0].lower().lstrip("/").split("@")[0]
            self._bg(self._on_command, chat_id, cmd)
            return

        ids = parse_device_input(text)[0]
        if ids:
            self._bg(self._add_devices, chat_id, ids)
            return

        # Cuma parse sebagai device list kalau semua token valid
        if looks_like_device_list(text):
            # Parse sekali di sini, pass result ke _add_devices biar nggak
            # double-parse. _add_devices tetap backward-compatible dengan string.
            parsed, _ = parse_device_input(text)
            self._bg(self._add_devices, chat_id, parsed)
            return

        # Teks biasa — jangan spam menu untuk setiap pesan.
        # Cuma reply kalau teksnya pendek (kemungkinan perintah).
        if len(text) < 50:
            self._bg(self._send_menu, chat_id)

    def _on_command(self, chat_id, cmd):
        if cmd in ("start", "menu"):
            self._send_menu(chat_id)
        elif cmd == "status":
            runs = self.registry.active(chat_id)
            if not runs:
                self.tg.send(chat_id, "Belum ada run aktif.")
            for r in runs:
                self._show_run_status(r)
        elif cmd == "stop":
            n = self.registry.stop_all(chat_id)
            self.tg.send(chat_id, f"⛔ {n} run distop.")
            self._send_menu(chat_id)
        elif cmd == "reset":
            self.devices.clear(chat_id)
            self.tg.send(chat_id, "🗑 List dikosongkan.")
            self._send_menu(chat_id)
        elif cmd == "help":
            self.tg.send(chat_id, ui.help_text())
        else:
            self.tg.send(chat_id, "Perintah nggak dikenal. /help")

    # ── input ──────────────────────────────────────────────────────────────
    def _on_document(self, chat_id, doc):
        name = doc.get("file_name") or "file.txt"
        local = self.tg.download(doc.get("file_id"), Path("./uploads"))
        if not local:
            self.tg.send(chat_id, "❌ Gagal ambil file")
            return
        try:
            content = Path(local).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            self.tg.send(chat_id, f"❌ Gagal baca file: {html.escape(str(e))}")
            return
        finally:
            try:
                os.remove(local)
            except Exception:
                pass
        ids = extract_device_ids(content)
        if not ids:
            self.tg.send(chat_id, f"❌ Nggak ada device id di <b>{name}</b>.")
            return
        self._add_devices(chat_id, ids, source=name)

    def _add_devices(self, chat_id, ids, source=None):
        if isinstance(ids, str):
            found, _ = parse_device_input(ids)
            ids = found
        if not ids:
            self.tg.send(chat_id, "❌ Nggak ada device id valid.")
            return
        total, added = self.devices.add(chat_id, ids)
        head = f"➕ <b>{added}</b> device ditambah"
        if source:
            head += f" dari <code>{source}</code>"
        head += f"\n📱 Total sekarang: <b>{total}</b>"
        self.tg.send(chat_id, head)
        self._send_menu(chat_id)

    def _start_run(self, chat_id):
        ids = self.devices.get(chat_id)
        if not ids:
            self.tg.send(chat_id, "❌ List device masih kosong. Tambah dulu.")
            self._send_menu(chat_id)
            return
        with self._run_lock:
            run = self.registry.create(chat_id, ids)
            text, kb = ui.run_status(run)
            r = self.tg.send(chat_id, text, kb)
            if not r:
                self.tg.send(chat_id, "❌ Gagal kirim pesan run.")
                return
            run.message_id = r
            # FIX: pass math.inf for unlimited, not 0
            executor = RunExecutor(run, math.inf, self.registry,
                                   on_finish=self._on_run_finish)
            executor.start()
            # NOTE: jangan clear device list di sini — user masih butuh
            # lihat/tambah device setelah run dimulai.

    def _on_run_finish(self, run):
        try:
            if run.stop:
                reason = "stopped"
            elif run.note:
                reason = "empty"
            else:
                reason = "done"
            snap = run.snapshot()
            snap["others"] = len(self.registry.active(run.chat_id))
            text = run_finished_text(snap, reason)
            if run.message_id:
                if not self.tg.edit(run.chat_id, run.message_id, text,
                                    keyboard=MENU_KB):
                    self.tg.send(run.chat_id, text, MENU_KB)
            else:
                self.tg.send(run.chat_id, text, MENU_KB)
        except Exception:
            pass

    # ── callbacks ──────────────────────────────────────────────────────────
    def _on_callback(self, q):
        chat_id = str((q.get("message") or {}).get("chat", {}).get("id", ""))
        data = q.get("data") or ""
        mid = (q.get("message") or {}).get("message_id")
        print(f"[KICKLUR] callback: chat={chat_id} data={data} mid={mid}", flush=True)
        self._bg(self.tg.answer, q["id"])
        if not self._allowed(chat_id):
            return
        # Handler alldev & togall di foreground — biar nggak ke-skip
        # pas container restart/redeploy
        if data.startswith("alldev:") or data.startswith("togall:"):
            self._do_callback(chat_id, mid, data)
        else:
            self._bg(self._do_callback, chat_id, mid, data)

    def _do_callback(self, chat_id, mid, data):
        try:
            self._dispatch_callback(chat_id, mid, data)
        except Exception as e:
            self.tg.send(chat_id, f"⚠️ {type(e).__name__}: {e}")

    def _dispatch_callback(self, chat_id, mid, data):
        if data == "menu":
            self._send_menu(chat_id, mid)

        elif data == "add":
            self.tg.send(chat_id,
                         "➕ Kirim device id (boleh banyak, satu per baris), "
                         "atau upload file <b>.txt</b>.")

        elif data == "list":
            text, kb = ui.device_list_view(self.devices.get(chat_id))
            self.tg.edit(chat_id, mid, text, kb)

        elif data.startswith("list:"):
            page = int(data.split(":")[1])
            text, kb = ui.device_list_view(self.devices.get(chat_id), page=page)
            self.tg.edit(chat_id, mid, text, kb)

        elif data == "reset":
            self.devices.clear(chat_id)
            self.tg.send(chat_id, "🗑 List dikosongkan.")
            self._send_menu(chat_id, mid)

        elif data == "stopall":
            n = self.registry.stop_all(chat_id)
            self.tg.send(chat_id, f"⛔ {n} run distop.")
            self._send_menu(chat_id, mid)

        elif data == "info":
            text, kb = ui.info_view()
            self.tg.edit(chat_id, mid, text, kb)

        elif data == "status":
            runs = self.registry.active(chat_id)
            if not runs:
                self.tg.send(chat_id, "Belum ada run aktif.")
            for r in runs:
                self._show_run_status(r)

        elif data == "start":
            if not self.devices.get(chat_id):
                self.tg.send(chat_id, "❌ List device masih kosong.")
                return
            self._start_run(chat_id)

        elif data == "settings":
            self.tg.send(chat_id, "⚙️ Setelan belum tersedia. Gunakan .env untuk konfigurasi.")

        elif data.startswith("st:"):
            run = self.registry.get(chat_id, int(data.split(":")[1]))
            if run:
                self._show_run_status(run, mid)

        elif data.startswith("dev:"):
            parts = data.split(":")
            run = self.registry.get(chat_id, int(parts[1]))
            page = int(parts[2]) if len(parts) > 2 else 0
            if run:
                self._show_run_device_panel(run, mid, page=page)

        elif data.startswith("tog:"):
            _, rid, idx = data.split(":")
            run = self.registry.get(chat_id, int(rid))
            if not run:
                return
            i = int(idx)
            if not (0 <= i < len(run.devices)):
                self.tg.send(chat_id, "⚠️ Device itu nggak ada lagi di run ini.")
                return
            did = run.devices[i]
            run.toggle_pause(did)
            # Tampilkan ulang panel di halaman yang sama (device sama, page sama)
            self._show_run_device_panel(run, mid, page=ui.page_of_device(run, i))

        elif data.startswith("pauseall:"):
            rid = int(data.split(":")[1])
            run = self.registry.get(chat_id, rid)
            if run:
                for d in run.devices:
                    run.pause_device(d)
                self.tg.send(chat_id, f"⏸ Semua device dipause.")
                self._show_run_status(run, mid)

        elif data.startswith("resumeall:"):
            rid = int(data.split(":")[1])
            run = self.registry.get(chat_id, rid)
            if run:
                for d in run.devices:
                    run.resume_device(d)
                self.tg.send(chat_id, f"▶️ Semua device dilanjut.")
                self._show_run_status(run, mid)

        elif data.startswith("stop:"):
            rid = int(data.split(":")[1])
            run = self.registry.get(chat_id, rid)
            if run:
                run.request_stop()
                self.tg.send(chat_id, f"⛔ Run #{rid} dihentikan.")
                self._show_run_status(run, mid)

        elif data.startswith("nav:"):
            # Navigasi ke run lain tanpa scroll
            rid = int(data.split(":")[1])
            run = self.registry.get(chat_id, rid)
            if run:
                self._show_run_status(run, mid)

        elif data.startswith("alldev:"):
            # Panel Semua Device — semua run sekaligus, pause dari mana aja
            try:
                print(f"[KICKLUR] alldev: mulai", flush=True)
                page = int(data.split(":")[1])
                runs = self.registry.active(chat_id)
                print(f"[KICKLUR] alldev: {len(runs)} run aktif", flush=True)
                text, kb = ui.all_runs_device_panel(runs, page=page)
                print(f"[KICKLUR] alldev: panel dibuat, text={len(text)} chars, kb={len(kb)} rows", flush=True)
                ok = self.tg.edit(chat_id, mid, text, kb)
                print(f"[KICKLUR] alldev: edit result={ok}", flush=True)
                if not ok:
                    print(f"[KICKLUR] alldev: editMessageText GAGAL — coba send baru", flush=True)
                    self.tg.send(chat_id, text, kb)
            except Exception as e:
                print(f"[KICKLUR] alldev error: {type(e).__name__}: {e}", flush=True)
                self.tg.send(chat_id, f"⚠️ Error buka panel: {type(e).__name__}: {e}")

        elif data.startswith("togall:"):
            # Toggle pause device di SEMUA run sekaligus
            try:
                did = data.split(":", 1)[1]
                runs = self.registry.active(chat_id)
                for run in runs:
                    if did in run.devices:
                        run.toggle_pause(did)
                # Refresh panel
                text, kb = ui.all_runs_device_panel(runs, page=0)
                self.tg.edit(chat_id, mid, text, kb)
            except Exception as e:
                print(f"[KICKLUR] togall error: {type(e).__name__}: {e}", flush=True)
                self.tg.send(chat_id, f"⚠️ Error pause: {type(e).__name__}: {e}")

        elif data == "noop":
            pass

    # ── main loop ──────────────────────────────────────────────────────────
    def _bg(self, fn, *args, **kwargs):
        """Run a Telegram-touching function off the polling thread."""
        threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()

    def poll(self):
        print(f"[KICKLUR] polling started", flush=True)
        while True:
            try:
                data = self.tg.get_updates(self.offset + 1)
                if not data.get("ok"):
                    time.sleep(2)
                    continue
                for upd in data.get("result", []):
                    self.offset = max(self.offset, upd.get("update_id", 0))
                    try:
                        self.handle(upd)
                    except Exception as e:
                        print(f"[KICKLUR] handler error: {type(e).__name__}: {e}",
                              flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"[KICKLUR] poll error: {type(e).__name__}: {e}", flush=True)
                time.sleep(3)
