"""All rendering. Pure functions: state in, text + keyboard out.

Kept apart from bot.py so the message layout can be changed without touching
dispatch logic.
"""
from . import config as cfg
from .runs import DONE, PAUSED, RUNNING, STOPPED

BAR = "─" * 34


def _fmt_dur(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}j {m:02d}m"


def _now():
    import time
    return time.time()


# ── menu ───────────────────────────────────────────────────────────────────
def main_menu(n_devices, active_runs):
    text = (
        "⚡ <b>KICKLUR</b> — BF Kicker\n"
        f"<code>{BAR}</code>\n"
        f"📱 Device   : <b>{n_devices}</b>\n"
        f"▶️ Run aktif: <b>{active_runs}</b>\n"
        f"<code>{BAR}</code>\n"
        f"Worker <b>{cfg.BF_WORKERS}</b>/run · maks <b>{cfg.BF_MAX_CONCURRENCY}</b> · "
        f"jeda <b>{cfg.BF_KICK_DELAY}s</b>"
    )
    kb = {"inline_keyboard": [
        [{"text": "▶️ START KICK", "callback_data": "start"}],
        [{"text": "➕ Tambah Device", "callback_data": "add"},
         {"text": "📋 Lihat List", "callback_data": "list"}],
        [{"text": "📊 Status", "callback_data": "status"},
         {"text": "⛔ STOP SEMUA", "callback_data": "stopall"}],
        [{"text": "🗑 Reset List", "callback_data": "reset"},
         {"text": "ℹ️ Info", "callback_data": "info"}],
    ]}
    return text, kb


def ask_loops():
    text = (
        "🔁 <b>Berapa loop?</b>\n"
        f"<code>{BAR}</code>\n"
        "1 loop = 1 kick. Device diputar bergantian.\n\n"
        "Kirim angka, atau <b>0</b> untuk <b>unlimited</b>."
    )
    return text, {"inline_keyboard": [[{"text": "« Batal", "callback_data": "menu"}]]}


def ask_delay():
    text = (
        "⏱ <b>Jeda antar kick?</b>\n"
        f"<code>{BAR}</code>\n"
        f"Default <b>{cfg.BF_KICK_DELAY}s</b>.\n\n"
        "Kirim angka detik (contoh <code>0.5</code>), "
        "atau <b>0</b> untuk tanpa jeda."
    )
    return text, {"inline_keyboard": [[{"text": "« Batal", "callback_data": "menu"}]]}


# ── run panel ──────────────────────────────────────────────────────────────
def run_header(run):
    if run.status == DONE:
        state = "✅ SELESAI"
    elif run.status == STOPPED:
        state = "⏹ DISTOP"
    elif run.all_paused:
        state = "⏸ PAUSE"
    else:
        state = "▶️ JALAN"
    return (f"⚡ <b>KICKLUR</b> · Run #{run.run_id} · {state}\n"
            f"<code>{BAR}</code>")


def run_status(run):
    lines = [run_header(run)]
    prog = f"{run.kicks}/{run.total_loops}" if run.total_loops else f"{run.kicks}/∞"
    n_run = len(run.running_devices())
    n_pause = len(run.paused_devices())
    n_stop = len(run.live_devices()) - n_run - n_pause
    lines.append(
        f"🎯 Kick    : <b>{prog}</b>\n"
        f"✅ Sukses  : <b>{run.ok}</b>   ❌ Gagal: <b>{run.fail}</b>\n"
        f"⚡ Speed   : <b>{run.speed:.1f}</b>/s   📶 <b>{run.avg_ms:.0f}</b>ms\n"
        f"⏱ Durasi  : <b>{_fmt_dur(run.elapsed)}</b>   "
        f"👷 <b>{max(0, run.workers_alive)}</b> worker\n"
        f"📱 Device  : 🟢 <b>{n_run}</b>  ⏸ <b>{n_pause}</b>  ⏹ <b>{n_stop}</b>"
    )
    if run.fail and run.kicks and run.fail / run.kicks >= 0.5:
        lines.append(f"⚠️ <b>{run.fail / run.kicks * 100:.0f}% gagal</b> — server "
                     f"mungkin nolak. Turunkan worker atau naikkan jeda.")
    if n_pause:
        lines.append(f"⏸ <b>{n_pause} device</b> sedang pause (nggak di-kick).")
    lines.append(f"<code>{BAR}</code>")
    lines.append("Buka 📋 Device buat pause/lanjut per device.")

    rows = [
        [{"text": "📋 Device (pause/lanjut)", "callback_data": f"dev:{run.run_id}"}],
        [{"text": "📊 Refresh", "callback_data": f"st:{run.run_id}"},
         {"text": "⛔ STOP RUN", "callback_data": f"stop:{run.run_id}"}],
    ]
    if n_pause:
        rows.append([{"text": "▶️ Lanjut semua", "callback_data": f"resumeall:{run.run_id}"}])
    else:
        rows.append([{"text": "⏸ Pause semua", "callback_data": f"pauseall:{run.run_id}"}])
    rows.append([{"text": "🏠 Menu", "callback_data": "menu"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def device_panel(run, page=0, per_page=8):
    """Per-device pause/resume panel. Buttons carry the device INDEX in
    run.order, which is stable for the life of the run."""
    devs = [run.devices[d] for d in run.order]
    total = len(devs)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    start = page * per_page
    chunk = devs[start:start + per_page]

    n_run = len(run.running_devices())
    n_pause = len(run.paused_devices())
    lines = [
        f"📋 <b>Device</b> · Run #{run.run_id}\n"
        f"<code>{BAR}</code>\n"
        f"🟢 jalan <b>{n_run}</b> · ⏸ pause <b>{n_pause}</b> · "
        f"total <b>{total}</b>\n"
        f"<code>{BAR}</code>"
    ]
    for i, d in enumerate(chunk, start=start):
        name = d.nick or (str(d.acc) if d.acc else "—")
        lines.append(
            f"{i + 1}. {d.mark} <code>…{d.device_id[-10:]}</code>\n"
            f"     {name} · kick <b>{d.kicks}</b> (✅{d.ok} ❌{d.fail})"
        )
    lines.append(f"<code>{BAR}</code>")
    lines.append("<b>Klik tombol device → pause/lanjut.</b>")

    rows = []
    for i, d in enumerate(chunk, start=start):
        label = f"{d.mark} {i + 1}. …{d.device_id[-8:]}"
        rows.append([{"text": label, "callback_data": f"tog:{run.run_id}:{i}"}])
    if pages > 1:
        rows.append([
            {"text": "« Prev", "callback_data": f"dev:{run.run_id}:{page-1}"},
            {"text": f"{page+1}/{pages}", "callback_data": "noop"},
            {"text": "Next »", "callback_data": f"dev:{run.run_id}:{page+1}"},
        ])
    rows.append([{"text": "📊 Status", "callback_data": f"st:{run.run_id}"},
                 {"text": "🏠 Menu", "callback_data": "menu"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def device_list_view(devices, page=0, per_page=20):
    total = len(devices)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    chunk = devices[page * per_page:(page + 1) * per_page]
    lines = [f"📋 <b>Device List</b>\n<code>{BAR}</code>",
             f"Total <b>{total}</b> · hal {page + 1}/{pages}\n"]
    for i, did in enumerate(chunk, start=page * per_page + 1):
        lines.append(f"{i}. <code>…{did[-18:]}</code>")
    if not devices:
        lines.append("(kosong — kirim device id atau upload .txt)")
    kb_rows = []
    nav = []
    if pages > 1:
        nav.append({"text": "« Prev", "callback_data": f"list:{page-1}"})
        nav.append({"text": f"{page+1}/{pages}", "callback_data": "noop"})
        nav.append({"text": "Next »", "callback_data": f"list:{page+1}"})
    if nav:
        kb_rows.append(nav)
    kb_rows.append([{"text": "🏠 Menu", "callback_data": "menu"}])
    return "\n".join(lines), {"inline_keyboard": kb_rows}


def info_view():
    text = (
        "ℹ️ <b>KickLur</b>\n"
        f"<code>{BAR}</code>\n"
        "Port setia dari <code>brutetolslhoya</code>.\n\n"
        f"Login  : <code>{cfg.HOST}:{cfg.PORT}</code>\n"
        f"CLI    : <code>{cfg.VER}</code>\n"
        f"Channel: <code>{cfg.CHAN}</code>\n\n"
        f"Kick timeout : <b>{cfg.KICK_TIMEOUT}s</b>\n"
        f"Open timeout : <b>{cfg.OPEN_TIMEOUT}s</b>\n"
        f"Fetch retry  : <b>{cfg.FETCH_ATTEMPTS}x</b>\n"
        f"Worker/run   : <b>{cfg.BF_WORKERS}</b>\n"
        f"Maks total   : <b>{cfg.BF_MAX_CONCURRENCY}</b>\n"
        f"Jeda default : <b>{cfg.BF_KICK_DELAY}s</b>\n"
        f"Lookup nama  : <b>{'on' if cfg.BF_LOOKUP else 'off'}</b>\n\n"
        "<b>Arti tanda</b>\n"
        "🟢 jalan · ⏸ pause · ✅ pernah sukses · ⏹ distop"
    )
    return text, {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "menu"}]]}


def help_text():
    return (
        "⚡ <b>KICKLUR</b>\n"
        f"<code>{BAR}</code>\n"
        "<b>Cara pakai</b>\n"
        "1. Tambah device (kirim id, atau upload .txt)\n"
        "2. START KICK\n"
        "3. Isi loop (0 = unlimited)\n"
        "4. Isi jeda (0.5 = default)\n"
        "5. Buka 📋 Device buat pause/lanjut per device\n\n"
        "<b>Perintah</b>\n"
        "/start — menu\n"
        "/menu — menu\n"
        "/status — status run\n"
        "/stop — stop semua\n"
        "/reset — kosongkan list\n"
        "/help — ini"
    )
