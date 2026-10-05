"""Update dispatch: Telegram update -> action.

Owner-only. State per chat: the device list, the wizard step, and the runs.

Two rules that keep the UI responsive and correct:

  * every Telegram call runs on a short-lived worker thread, so a slow edit
    never blocks the polling loop — that is what made a pause tap look like it
    "did nothing" while a run was busy;
  * a device is addressed by its device id, never by its list position, so a
    pause lands on the right device even if the list changes between render
    and tap.
"""
import threading
import time

from . import config as cfg
from . import ui
from .devices import DeviceList, extract_ids, is_device_id
from .runs import DONE, PAUSED, REGISTRY, RUNNING, STOPPED
from .telegram import Telegram

MENU = "menu"
WANT_LOOPS = "want_loops"
WANT_DELAY = "want_delay"


class KickLurBot:
    def __init__(self, tg: Telegram):
        self.tg = tg
        self.devices = DeviceList()
        self.step = {}                 # chat_id -> wizard step
        self._pending_loops = {}       # chat_id -> loops chosen in the wizard
        self._watch_lock = threading.Lock()
        self._watching = set()         # run_ids already being watched

    # ── helpers ────────────────────────────────────────────────────────────
    def _allowed(self, chat_id):
        return str(chat_id) in cfg.ALLOWED_IDS

    def _bg(self, fn, *args, **kwargs):
        """Run a Telegram-touching function off the polling thread."""
        threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()

    def _send_menu(self, chat_id, message_id=None):
        n = len(self.devices.get(chat_id))
        active = len(REGISTRY.active_runs(chat_id))
        text, kb = ui.main_menu(n, active)
        if message_id:
            r = self.tg.edit(chat_id, message_id, text, kb, parse_mode="HTML")
            if r.get("ok"):
                return message_id
        r = self.tg.send(chat_id, text, kb, parse_mode="HTML")
        if r.get("ok"):
            return r["result"]["message_id"]
        return None

    def _show_run_status(self, run, message_id=None):
        text, kb = ui.run_status(run)
        if message_id:
            self.tg.edit(run.chat_id, message_id, text, kb, parse_mode="HTML")
        else:
            self.tg.send(run.chat_id, text, kb, parse_mode="HTML")

    def _show_run_results(self, run, message_id, page=0):
        text, kb = ui.run_results(run, page=page)
        self.tg.edit(run.chat_id, message_id, text, kb, parse_mode="HTML")

    # ── live watcher ───────────────────────────────────────────────────────
    def _watch(self, run, message_id):
        """Refresh the run panel while it runs; finalise when it ends."""
        with self._watch_lock:
            if run.run_id in self._watching:
                return
            self._watching.add(run.run_id)

        def _loop():
            try:
                last = 0.0
                while run.status in (RUNNING, PAUSED):
                    time.sleep(1.0)
                    now = time.time()
                    if run.status == RUNNING and now - last >= 5.0:
                        last = now
                        self._show_run_status(run, message_id)
                self._show_run_status(run, message_id)
            except Exception:
                pass
            finally:
                with self._watch_lock:
                    self._watching.discard(run.run_id)

        threading.Thread(target=_loop, name=f"watch-{run.run_id}", daemon=True).start()

    # ── dispatch ───────────────────────────────────────────────────────────
    def handle(self, upd):
        if "message" in upd:
            self._on_message(upd["message"])
        elif "callback_query" in upd:
            self._on_callback(upd["callback_query"])

    def _on_message(self, msg):
        chat_id = msg["chat"]["id"]
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

        step = self.step.get(chat_id, MENU)
        if step == WANT_LOOPS:
            self._bg(self._on_loops, chat_id, text)
            return
        if step == WANT_DELAY:
            self._bg(self._on_delay, chat_id, text)
            return

        ids = extract_ids(text) or ([text] if is_device_id(text) else [])
        if ids:
            self._bg(self._add_devices, chat_id, ids)
            return

        self._bg(self._send_menu, chat_id)

    def _on_command(self, chat_id, cmd):
        if cmd in ("start", "menu"):
            self.step[chat_id] = MENU
            self._send_menu(chat_id)
        elif cmd == "status":
            runs = REGISTRY.active_runs(chat_id)
            if not runs:
                self.tg.send(chat_id, "Belum ada run aktif.")
            for r in runs:
                self._show_run_status(r)
        elif cmd == "stop":
            n = REGISTRY.stop_all(chat_id)
            self.tg.send(chat_id, f"⛔ {n} run distop.")
            self._send_menu(chat_id)
        elif cmd == "reset":
            n = self.devices.reset(chat_id)
            self.tg.send(chat_id, f"🗑 {n} device dihapus.")
            self._send_menu(chat_id)
        elif cmd == "help":
            self.tg.send(chat_id, ui.help_text(), parse_mode="HTML")
        else:
            self.tg.send(chat_id, "Perintah nggak dikenal. /help")

    # ── wizard ─────────────────────────────────────────────────────────────
    def _on_loops(self, chat_id, text):
        try:
            loops = int(text)
        except ValueError:
            self.tg.send(chat_id, "Kirim angka ya (0 = unlimited).")
            return
        self._pending_loops[chat_id] = max(0, loops)
        self.step[chat_id] = WANT_DELAY
        t, kb = ui.ask_delay()
        self.tg.send(chat_id, t, kb, parse_mode="HTML")

    def _on_delay(self, chat_id, text):
        try:
            delay = float(text)
        except ValueError:
            self.tg.send(chat_id, "Kirim angka detik ya (contoh 0.5).")
            return
        self.step[chat_id] = MENU
        loops = self._pending_loops.pop(chat_id, 0)
        self._start_run(chat_id, max(0.0, delay), loops)

    def _on_document(self, chat_id, doc):
        name = doc.get("file_name") or "file.txt"
        size = doc.get("file_size") or 0
        if size > 20 * 1024 * 1024:
            self.tg.send(chat_id, "❌ File terlalu besar (maks 20 MB).")
            return
        data = self.tg.get_file(doc["file_id"])
        if not data:
            self.tg.send(chat_id, "❌ Gagal ambil file dari Telegram.")
            return
        text = data.decode("utf-8", errors="ignore")
        ids = extract_ids(text)
        if not ids:
            self.tg.send(chat_id, f"❌ Nggak ada device id di <b>{name}</b>.",
                         parse_mode="HTML")
            return
        self._add_devices(chat_id, ids, source=name)

    def _add_devices(self, chat_id, ids, source=None):
        added, total, capped = self.devices.add(chat_id, ids)
        head = f"➕ <b>{added}</b> device ditambah"
        if source:
            head += f" dari <code>{source}</code>"
        head += f"\n📱 Total sekarang: <b>{total}</b>"
        if capped:
            head += f"\n⚠️ Batas {cfg.BF_MAX_DEVICES} device tercapai."
        self.tg.send(chat_id, head, parse_mode="HTML")
        self._send_menu(chat_id)

    def _start_run(self, chat_id, delay, loops):
        ids = self.devices.get(chat_id)
        if not ids:
            self.tg.send(chat_id, "❌ List device masih kosong. Tambah dulu.")
            self._send_menu(chat_id)
            return
        run = REGISTRY.create(chat_id, 0, ids, loops, delay)
        text, kb = ui.run_status(run)
        r = self.tg.send(chat_id, text, kb, parse_mode="HTML")
        if not r.get("ok"):
            self.tg.send(chat_id, "❌ Gagal kirim pesan run.")
            return
        run.message_id = r["result"]["message_id"]
        REGISTRY.start(run, want_lookup=cfg.BF_LOOKUP)
        self._watch(run, run.message_id)

    # ── callbacks ──────────────────────────────────────────────────────────
    def _on_callback(self, q):
        chat_id = q["message"]["chat"]["id"]
        data = q.get("data") or ""
        mid = q["message"]["message_id"]
        # answer first so the button never looks dead, then do the real work
        self._bg(self.tg.answer_callback, q["id"])
        if not self._allowed(chat_id):
            return
        self._bg(self._do_callback, chat_id, mid, data)

    def _do_callback(self, chat_id, mid, data):
        try:
            self._dispatch_callback(chat_id, mid, data)
        except Exception as e:
            self.tg.send(chat_id, f"⚠️ {type(e).__name__}: {e}")

    def _dispatch_callback(self, chat_id, mid, data):
        if data == "menu":
            self.step[chat_id] = MENU
            self._send_menu(chat_id, mid)

        elif data == "add":
            self.tg.send(chat_id,
                         "➕ Kirim device id (boleh banyak, satu per baris), "
                         "atau upload file <b>.txt</b>.", parse_mode="HTML")

        elif data == "list":
            text, kb = ui.device_list_view(self.devices.get(chat_id))
            self.tg.edit(chat_id, mid, text, kb, parse_mode="HTML")

        elif data.startswith("list:"):
            page = int(data.split(":")[1])
            text, kb = ui.device_list_view(self.devices.get(chat_id), page=page)
            self.tg.edit(chat_id, mid, text, kb, parse_mode="HTML")

        elif data == "reset":
            n = self.devices.reset(chat_id)
            self.tg.send(chat_id, f"🗑 {n} device dihapus.")
            self._send_menu(chat_id, mid)

        elif data == "stopall":
            n = REGISTRY.stop_all(chat_id)
            self.tg.send(chat_id, f"⛔ {n} run distop.")
            self._send_menu(chat_id, mid)

        elif data == "info":
            text, kb = ui.info_view()
            self.tg.edit(chat_id, mid, text, kb, parse_mode="HTML")

        elif data == "status":
            runs = REGISTRY.active_runs(chat_id)
            if not runs:
                self.tg.send(chat_id, "Belum ada run aktif.")
            for r in runs:
                self._show_run_status(r)

        elif data == "start":
            if not self.devices.get(chat_id):
                self.tg.send(chat_id, "❌ List device masih kosong.")
                return
            self.step[chat_id] = WANT_LOOPS
            text, kb = ui.ask_loops()
            self.tg.edit(chat_id, mid, text, kb, parse_mode="HTML")

        elif data.startswith("st:"):
            run = REGISTRY.get(int(data.split(":")[1]))
            if run:
                self._show_run_status(run, mid)

        elif data.startswith("res:"):
            parts = data.split(":")
            run = REGISTRY.get(int(parts[1]))
            page = int(parts[2]) if len(parts) > 2 else 0
            if run:
                self._show_run_results(run, mid, page=page)

        elif data.startswith("dev:"):
            parts = data.split(":")
            run = REGISTRY.get(int(parts[1]))
            page = int(parts[2]) if len(parts) > 2 else 0
            if run:
                text, kb = ui.run_device_panel(run, page=page)
                self.tg.edit(chat_id, mid, text, kb, parse_mode="HTML")

        # ── pause / resume ────────────────────────────────────────────────
        elif data.startswith("tog:"):
            _, rid, idx = data.split(":")
            run = REGISTRY.get(int(rid))
            if not run:
                return
            i = int(idx)
            if not (0 <= i < len(run.order)):
                self.tg.send(chat_id, "⚠️ Device itu nggak ada lagi di run ini.")
                return
            did = run.order[i]                      # id, not position
            REGISTRY.toggle_device(run.run_id, did)
            # re-render the panel page that actually holds this device
            text, kb = ui.run_device_panel(run, page=ui.page_of_device(run, i))
            self.tg.edit(chat_id, mid, text, kb, parse_mode="HTML")

        elif data.startswith("pauseall:"):
            rid = int(data.split(":")[1])
            n = REGISTRY.set_all_paused(rid, True)
            self.tg.send(chat_id, f"⏸ {n} device dipause.")
            run = REGISTRY.get(rid)
            if run:
                self._show_run_status(run, mid)

        elif data.startswith("resumeall:"):
            rid = int(data.split(":")[1])
            n = REGISTRY.resume_all(rid)
            self.tg.send(chat_id, f"▶️ {n} device dilanjut.")
            run = REGISTRY.get(rid)
            if run:
                self._show_run_status(run, mid)

        elif data.startswith("stop:"):
            rid = int(data.split(":")[1])
            REGISTRY.stop(rid)
            run = REGISTRY.get(rid)
            if run:
                self._show_run_status(run, mid)

        elif data == "noop":
            pass

    # ── main loop ──────────────────────────────────────────────────────────
    def poll_forever(self):
        while True:
            try:
                for upd in self.tg.poll():
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
