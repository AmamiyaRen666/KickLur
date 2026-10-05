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


def main_menu(n_devices, active_runs):
    text = (
        "⚡ <b>KICKLUR</b> — BF Kicker\n"
        f"<code>{BAR}</code>\n"
        f"📱 Device di list : <b>{n_devices}</b>\n"
        f"▶️ Run aktif       : <b>{active_runs}</b>\n"
        f"<code>{BAR}</code>\n"
        f"Worker/run <b>{cfg.BF_WORKERS}</b> · "
        f"maks total <b>{cfg.BF_MAX_CONCURRENCY}</b>\n"
        f"Pace <b>{cfg.BF_KICK_DELAY}s</b> · CLI <code>{cfg.VER}</code>"
    )
    kb = {
        "inline_keyboard": [
            [{"text": "▶️ START KICK", "callback_data": "start"}],
            [{"text": "➕ Tambah Device", "callback_data": "add"},
             {"text": "📋 Lihat List", "callback_data": "list"}],
            [{"text": "📊 Status", "callback_data": "status"},
             {"text": "⛔ STOP SEMUA", "callback_data": "stopall"}],
            [{"text": "🗑 Reset List", "callback_data": "reset"},
             {"text": "ℹ️ Info", "callback_data": "info"}],
        ]
    }
    return text, kb


def ask_loops():
    text = (
        "🔁 <b>Berapa loop?</b>\n"
        f"<code>{BAR}</code>\n"
        "1 loop = 1 kick. Device diputar bergantian.\n\n"
        "Kirim angka, atau ketik <b>0</b> untuk <b>unlimited</b>."
    )
    return text, {"inline_keyboard": [[{"text": "« Batal", "callback_data": "menu"}]]}


def ask_delay():
    text = (
        "⏱ <b>Jeda antar kick?</b>\n"
        f"<code>{BAR}</code>\n"
        f"Default <b>{cfg.BF_KICK_DELAY}s</b> per worker.\n\n"
        "Kirim angka detik (contoh <code>0.5</code>), "
        "atau <b>0</b> untuk tanpa jeda."
    )
    return text, {"inline_keyboard": [[{"text": "« Batal", "callback_data": "menu"}]]}


def run_header(run):
    if run.status == DONE:
        state = "✅ SELESAI"
    elif run.status == STOPPED:
        state = "⏹ DISTOP"
    elif run.all_paused():
        state = "⏸ SEMUA PAUSE"
    else:
        state = "▶️ JALAN"
    return (
        f"⚡ <b>KICKLUR</b> · Run #{run.run_id} · {state}\n"
        f"<code>{BAR}</code>"
    )


def run_status(run):
    el = (run.finished or 0) - run.started if run.finished else _now() - run.started
    lines = [run_header(run)]
    prog = f"{run.kicks}/{run.total_loops}" if run.total_loops else f"{run.kicks}/∞"
    lines.append(
        f"🎯 Kick      : <b>{prog}</b>\n"
        f"✅ Sukses    : <b>{run.ok}</b>   ❌ Gagal : <b>{run.fail}</b>\n"
        f"⚡ Speed     : <b>{run.speed:.1f}</b> kick/s\n"
        f"📶 Latensi   : <b>{run.avg_ms:.0f}</b> ms\n"
        f"⏱ Durasi    : <b>{_fmt_dur(el)}</b>\n"
        f"👷 Worker    : <b>{max(0, run.workers_alive)}</b> aktif\n"
        f"📱 Device    : <b>{len(run.order)}</b>"
    )
    if run.fail and run.kicks:
        pct = run.fail / run.kicks * 100
        if pct >= 50:
            lines.append(f"⚠️ <b>{pct:.0f}% gagal</b> — server mungkin nolak, "
                         f"turunkan worker atau naikkan jeda.")
    lines.append(f"<code>{BAR}</code>")
    lines.append("Klik device di Hasil buat pause/lanjut.")
    kb = {"inline_keyboard": [
        [{"text": "📊 Refresh", "callback_data": f"st:{run.run_id}"}],
        [{"text": "📋 Hasil", "callback_data": f"res:{run.run_id}"}],
        [{"text": "⛔ STOP RUN INI", "callback_data": f"stop:{run.run_id}"}],
        [{"text": "🏠 Menu", "callback_data": "menu"}],
    ]}
    return "\n".join(lines), kb


def _now():
    import time
    return time.time()


def run_results(run, page=0, per_page=10):
    devs = [run.devices[d] for d in run.order]
    total = len(devs)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    chunk = devs[page * per_page:(page + 1) * per_page]

    lines = [
        f"📋 <b>Hasil</b> · Run #{run.run_id}\n"
        f"<code>{BAR}</code>\n"
        f"Hal {page + 1}/{pages} · {total} device\n"
    ]
    if not chunk:
        lines.append("(belum ada device)")
    for d in chunk:
        name = d.nick or (str(d.acc) if d.acc else "—")
        short = d.device_id[-12:]
        lines.append(
            f"{d.mark} <code>…{short}</code>\n"
            f"    {name}\n"
            f"    kick {d.kicks} · ✅{d.ok} ❌{d.fail}"
            + (f" · {d.last_ms:.0f}ms" if d.last_ms else "")
        )
    lines.append(f"<code>{BAR}</code>")
    lines.append("Klik device → pause/lanjut (sementara).")

    rows = []
    for d in chunk:
        rows.append([{
            "text": f"{d.mark} {d.device_id[-10:]}",
            "callback_data": f"tog:{run.run_id}:{run.order.index(d.device_id)}",
        }])
    nav = []
    if pages > 1:
        nav.append({"text": "« Prev", "callback_data": f"res:{run.run_id}:{page-1}"})
        nav.append({"text": f"{page+1}/{pages}", "callback_data": "noop"})
        nav.append({"text": "Next »", "callback_data": f"res:{run.run_id}:{page+1}"})
    rows.append(nav) if nav else None
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
        "<b>Arti tombol</b>\n"
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
        "4. Isi jeda (Enter = default)\n\n"
        "<b>Perintah</b>\n"
        "/start — menu\n"
        "/menu — menu\n"
        "/status — status run\n"
        "/stop — stop semua\n"
        "/reset — kosongkan list\n"
        "/help — ini"
    )
