"""Question-dispatch bot: devices in, runs out."""
import html
import os
import threading
import time
from pathlib import Path

from . import config as cfg
from . import ui
from .devices import (DeviceListStore, extract_device_ids,
                      looks_like_device_list, mentions_device_token,
                      parse_device_input)
from .runs import RunExecutor, RunRegistry
from .telegram import Telegram

HELP = (
    "💥 <b>KICKLUR — BF KICKER</b>\n"
    "━━━━━━━━━━━━━━━\n"
    "<b>Cara pakai</b>\n"
    "1. Kirim device id (teks atau file .txt) — list-nya <b>nambah</b>, nggak nimpa\n"
    "2. Tekan ▶️ Mulai Kick → langsung jalan (unlimited)\n"
    "3. Tekan 🗑 Hapus buat pause/lanjut/hapus device\n\n"
    "<b>Command</b>\n"
    "/menu — menu utama\n"
    "/stop — stop semua run\n"
    "/help — bantuan ini"
)


class KickLurBot:
    def __init__(self, tg=None):
        self.tg = tg or Telegram(cfg.BOT_TOKEN)
        self.devices = DeviceListStore()
        self.registry = RunRegistry()
        self.offset = 0
        self.owner = str(cfg.OWNER_CHAT_ID)
        extra = {x.strip() for x in os.environ.get("TG_ALLOWED_IDS", "").split(",")
                 if x.strip()}
        self.allowed = {self.owner} | extra
        self.upload_dir = Path(os.environ.get("KICKLUR_UPLOAD_DIR", "./uploads"))
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._run_lock = threading.Lock()

    # ── access ─────────────────────────────────────────────────────────────
    def is_allowed(self, chat_id):
        return str(chat_id) in self.allowed

    # ── menus ──────────────────────────────────────────────────────────────
    def show_home(self, chat_id, message_id=None):
        count = self.devices.count(chat_id)
        active = len(self.registry.active(chat_id))
        text = ui.home_text(count, active)
        kb = ui.home_kb(count, active)
        if message_id is None:
            self.tg.send(chat_id, text, kb)
        else:
            self.tg.edit(chat_id, message_id, text, kb)

    # ── runs ───────────────────────────────────────────────────────────────
    def start_run(self, chat_id):
        ids = self.devices.get(chat_id)
        if not ids:
            return None, "❌ Belum ada device di list"
        with self._run_lock:
            run = self.registry.create(chat_id, ids)
            snapshot = run.snapshot()
            mid = self.tg.send(chat_id, ui.run_text(snapshot), ui.run_kb(run.run_id))
            run.message_id = mid
            self.devices.clear(chat_id)
            executor = RunExecutor(run, 0, self.registry,
                                   on_finish=self._on_run_finish)
            executor.start()
        return run, None

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
            text = ui.run_finished_text(snap, reason)
            if run.message_id:
                if not self.tg.edit(run.chat_id, run.message_id, text,
                                    keyboard=ui.MENU_KB):
                    self.tg.send(run.chat_id, text, ui.MENU_KB)
            else:
                self.tg.send(run.chat_id, text, ui.MENU_KB)
            owner = self.owner
            if str(run.chat_id) != owner:
                self.tg.send(owner,
                             f"#\u20e3 Ringkasan run dari <code>{run.chat_id}</code>\n"
                             f"{text}")
        except Exception:
            pass

    # ── input ──────────────────────────────────────────────────────────────
    def add_devices(self, chat_id, text, source=""):
        found, unparsed = parse_device_input(text)
        if not found:
            self.tg.send(chat_id,
                         "❌ <b>Device ID tidak valid</b>\n\n"
                         "Format: <code>and_xxxx</code> atau <code>ios_xxxx</code>\n"
                         "Bisa banyak, dipisah spasi/koma — atau upload .txt.")
            return False
        total, added = self.devices.add(chat_id, found)
        ids = self.devices.get(chat_id)
        preview = "\n".join(f"  {i + 1}. <code>{ui._short(d, 34)}</code>"
                            for i, d in enumerate(ids[:5]))
        more = f"\n  … +{total - 5} lagi" if total > 5 else ""
        warn = f"\n⚠️ {len(unparsed)} token dilewati" if unparsed else ""
        head = f"📁 {html.escape(source)}\n" if source else ""
        self.tg.send(chat_id,
                     f"{ui.BANNER}\n"
                     f"━━━━━━━━━━━━━━━\n"
                     f"{head}"
                     f"➕ Baru masuk  : <b>{added}</b>\n"
                     f"📱 Total device: <b>{total}</b>\n\n"
                     f"{preview}{more}{warn}\n\n"
                     f"Kirim lagi untuk <b>nambah</b>, atau tekan ▶️ Mulai Kick.",
                     [[{"text": "▶️ Mulai Kick", "callback_data": "start"}],
                      [{"text": "🗑 Reset List", "callback_data": "reset"},
                       {"text": "🏠 Menu", "callback_data": "menu"}]])
        return True

    def handle_document(self, chat_id, doc):
        name = doc.get("file_name", "file.txt")
        local = self.tg.download(doc.get("file_id"), self.upload_dir)
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
            self.tg.send(chat_id, "❌ Tidak ada Device ID di file")
            return
        total, added = self.devices.add(chat_id, ids)
        self.tg.send(chat_id,
                     f"{ui.BANNER}\n"
                     f"━━━━━━━━━━━━━━━\n"
                     f"📁 {html.escape(name)}\n"
                     f"➕ Baru masuk  : <b>{added}</b>\n"
                     f"📱 Total device: <b>{total}</b>\n\n"
                     f"Kirim/upload lagi untuk <b>nambah</b>, atau tekan ▶️ Mulai Kick.",
                     [[{"text": "▶️ Mulai Kick", "callback_data": "start"}],
                      [{"text": "🗑 Reset List", "callback_data": "reset"},
                       {"text": "🏠 Menu", "callback_data": "menu"}]])

    # ── callbacks ──────────────────────────────────────────────────────────
    def handle_callback(self, cb):
        data = cb.get("data", "")
        msg = cb.get("message", {}) or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        mid = msg.get("message_id")
        cb_id = cb.get("id")

        if data == "noop":
            self.tg.answer(cb_id)
            return
        if not self.is_allowed(chat_id):
            self.tg.answer(cb_id, "🚫 Tidak diizinkan", True)
            return

        if data == "menu":
            self.tg.answer(cb_id)
            self.show_home(chat_id, mid)
            return

        if data == "start":
            count = self.devices.count(chat_id)
            if not count:
                self.tg.answer(cb_id, "❌ Belum ada device", True)
                return
            self.tg.answer(cb_id, "💥 Mulai kick (unlimited)")
            run, err = self.start_run(chat_id)
            if err:
                self.tg.answer(cb_id, err, True)
                return
            self.tg.edit(chat_id, mid,
                         f"💥 <b>RUN #{run.run_id} DIMULAI</b>\n"
                         f"━━━━━━━━━━━━━━━\n"
                         f"📱 {len(run.devices)} device · ∞ kick\n"
                         f"📊 Cek progress di pesan run.",
                         [[{"text": "📊 Status", "callback_data": f"status:{run.run_id}"}],
                          [{"text": "🏠 Menu", "callback_data": "menu"}]])
            return

        if data == "reset":
            self.tg.answer(cb_id, "🗑 List dikosongkan", True)
            self.devices.clear(chat_id)
            self.show_home(chat_id, mid)
            return

        if data.startswith("status:"):
            run = self.registry.get(chat_id, data.split(":", 1)[1])
            if not run:
                self.tg.answer(cb_id, "ℹ️ Run sudah selesai", True)
                return
            self.tg.answer(cb_id, "📊 Status diperbarui")
            self.tg.edit(chat_id, mid, ui.run_text(run.snapshot()),
                         ui.run_kb(run.run_id))
            return

        if data.startswith("hapus:"):
            run = self.registry.get(chat_id, data.split(":", 1)[1])
            if not run:
                self.tg.answer(cb_id, "ℹ️ Run sudah selesai", True)
                return
            self.tg.answer(cb_id)
            self.tg.edit(chat_id, mid, ui.hapus_text(run),
                         ui.hapus_kb(run, 0))
            return

        if data.startswith("hapuspage:"):
            parts = data.split(":")
            rid = parts[1] if len(parts) > 1 else ""
            try:
                page_n = int(parts[2])
            except (IndexError, ValueError):
                page_n = 0
            run = self.registry.get(chat_id, rid)
            if not run:
                self.tg.answer(cb_id, "ℹ️ Run sudah selesai", True)
                return
            self.tg.answer(cb_id)
            self.tg.edit(chat_id, mid, ui.hapus_text(run),
                         ui.hapus_kb(run, page_n))
            return

        if data.startswith("toggle:"):
            parts = data.split(":")
            rid = parts[1] if len(parts) > 1 else ""
            try:
                idx = int(parts[2])
            except (IndexError, ValueError):
                self.tg.answer(cb_id, "❌", True)
                return
            run = self.registry.get(chat_id, rid)
            if not run or not (0 <= idx < len(run.devices)):
                self.tg.answer(cb_id, "❌ Device tidak ditemukan", True)
                return
            device = run.devices[idx]
            state = run.toggle_pause(device)
            if state == "paused":
                self.tg.answer(cb_id, f"⏸ Device {idx + 1} di-pause — klik lagi buat lanjut", True)
            elif state == "running":
                self.tg.answer(cb_id, f"▶️ Device {idx + 1} jalan lagi", True)
            else:
                self.tg.answer(cb_id, f"⏹ Device {idx + 1} sudah dihapus", True)
            self.tg.edit(chat_id, mid, ui.hapus_text(run),
                         ui.hapus_kb(run, idx // ui.PAGE_SIZE))
            return

        if data.startswith("hapusall:"):
            rid = data.split(":", 1)[1]
            run = self.registry.get(chat_id, rid)
            if not run:
                self.tg.answer(cb_id, "ℹ️ Run sudah selesai", True)
                return
            n = 0
            for d in run.devices:
                if d not in run.stopped_set():
                    run.stop_device(d)
                    n += 1
            self.tg.answer(cb_id, f"🗑 {n} device dihapus" if n else "❌ Tidak ada device", True)
            self.tg.edit(chat_id, mid, ui.hapus_text(run),
                         ui.hapus_kb(run, 0))
            return

        self.tg.answer(cb_id)

    # ── messages ───────────────────────────────────────────────────────────
    def handle_message(self, msg):
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()

        if not self.is_allowed(chat_id):
            if text.startswith("/start"):
                self.tg.send(chat_id, "🚫 Bot ini privat.")
            return

        if text.startswith("/"):
            cmd = text.split()[0].lower().split("@")[0]
            if cmd in ("/start", "/menu"):
                self.show_home(chat_id)
            elif cmd == "/help":
                self.tg.send(chat_id, HELP)
            elif cmd == "/stop":
                n = self.registry.stop_all(chat_id)
                self.tg.send(chat_id, f"⛔ {n} run dihentikan" if n
                             else "❌ Tidak ada run aktif")
            else:
                self.tg.send(chat_id, "❓ Command tidak dikenal\n\n" + HELP)
            return

        if not text:
            return

        if looks_like_device_list(text) or mentions_device_token(text):
            self.add_devices(chat_id, text)
            return

        self.tg.send(chat_id,
                     "👋 Kirim device id (teks atau file .txt), atau /menu")

    # ── loop ───────────────────────────────────────────────────────────────
    def poll(self):
        print(f"[KICKLUR] polling started | owner={self.owner}", flush=True)
        while True:
            data = self.tg.get_updates(self.offset + 1)
            if not data.get("ok"):
                time.sleep(2)
                continue
            for upd in data.get("result", []):
                self.offset = max(self.offset, upd.get("update_id", 0))
                try:
                    if "callback_query" in upd:
                        self.handle_callback(upd["callback_query"])
                        continue
                    msg = upd.get("message") or upd.get("edited_message")
                    if not msg:
                        continue
                    doc = msg.get("document")
                    if doc:
                        chat_id = str((msg.get("chat") or {}).get("id", ""))
                        if self.is_allowed(chat_id):
                            self.handle_document(chat_id, doc)
                        continue
                    self.handle_message(msg)
                except Exception as e:
                    print(f"[KICKLUR] update error: {type(e).__name__}: {e}",
                          flush=True)
