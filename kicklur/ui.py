"""All rendering. Pure functions: state in, text + keyboard out."""
import html
import math
import time

from . import config as cfg

BAR = "─" * 34
PAGE_SIZE = 8


def _fmt_dur(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}j {m:02d}m"


# ── main menu ─────────────────────────────────────────────────────────────
def main_menu(n_devices, active_runs):
    text = (
        "⚡ <b>KICKLUR</b> — BF Kicker\n"
        f"<code>{BAR}</code>\n"
        f"📱 Device   : <b>{n_devices}</b>\n"
        f"▶️ Run aktif: <b>{active_runs}</b>\n"
        f"<code>{BAR}</code>\n"
        f"START KICK langsung jalan:\n"
        f"loop <b>∞</b> · jeda <b>{cfg.BF_KICK_DELAY}s</b> · "
        f"worker <b>{cfg.BF_WORKERS}</b>"
    )
    kb = [
        [{"text": "▶️ START KICK", "callback_data": "start"}],
        [{"text": "➕ Tambah Device", "callback_data": "add"},
         {"text": "📋 Lihat List", "callback_data": "list"}],
        [{"text": "📊 Status", "callback_data": "status"},
         {"text": "⛔ STOP SEMUA", "callback_data": "stopall"}],
        [{"text": "🗑 Reset List", "callback_data": "reset"},
         {"text": "⚙️ Setelan", "callback_data": "settings"}],
        [{"text": "ℹ️ Info", "callback_data": "info"}],
    ]
    return text, kb


# ── run status ────────────────────────────────────────────────────────────
def run_header(run):
    if run.finished:
        if run.stop:
            state = "⏹ DISTOP"
        elif run.note:
            state = "⏸ PAUSE"
        else:
            state = "✅ SELESAI"
    elif run.has_paused():
        state = "⏸ PAUSE"
    else:
        state = "▶️ JALAN"
    return (f"⚡ <b>KICKLUR</b> · Run #{run.run_id} · {state}\n"
            f"<code>{BAR}</code>")


def run_status(run, active_runs=None):
    lines = [run_header(run)]
    prog = f"{run.kicks}/{run.loops}" if run.loops else f"{run.kicks}/∞"
    n_run = run.active_count()
    n_pause = len(run.paused_set())
    n_stop = len(run.stopped_set())
    lines.append(
        f"🎯 Kick    : <b>{prog}</b>\n"
        f"✅ Sukses  : <b>{run.ok}</b>   ❌ Gagal: <b>{run.fail}</b>\n"
        f"⏱ Durasi  : <b>{_fmt_dur(time.time() - run.start)}</b>\n"
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
    # Tombol navigasi ke run lain (biar user nggak perlu scroll ke atas)
    if active_runs:
        nav_rows = run_nav_buttons(active_runs, run.run_id)
        rows.extend(nav_rows)
    return "\n".join(lines), rows


# ── device panel ──────────────────────────────────────────────────────────
def page_of_device(run, index, per_page=8):
    """Return page number that contains the given device index."""
    return index // per_page


def run_nav_buttons(active_runs, current_run_id):
    """Buat tombol navigasi ke semua run aktif (untuk pindah run tanpa scroll)."""
    if not active_runs:
        return []
    rows = []
    nav = []
    for r in active_runs:
        label = f"#{r.run_id}" + (" ←" if r.run_id == current_run_id else "")
        nav.append({"text": label, "callback_data": f"nav:{r.run_id}"})
    if len(nav) > 1:
        rows.append(nav)
    return rows


def device_panel(run, page=0, per_page=8, active_runs=None):
    """Per-device pause/resume panel with pagination."""
    devices = run.devices
    stopped = run.stopped_set()
    paused = run.paused_set()
    kicked = run.kicked_set()
    total = len(devices)

    n_run = total - len(stopped) - len(paused)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    start = page * per_page
    chunk = devices[start:start + per_page]

    lines = [
        f"📋 <b>Device</b> · <b>Run #{run.run_id}</b>\n"
        f"<code>{BAR}</code>\n"
        f"🟢 jalan <b>{n_run}</b> · ⏸ pause <b>{len(paused)}</b> · "
        f"⏹ hapus <b>{len(stopped)}</b> · total <b>{total}</b>\n"
        f"hal {page + 1}/{pages} · <b>klik tombol = pause/lanjut</b>\n"
        f"<code>{BAR}</code>"
    ]
    for i, did in enumerate(chunk, start=start):
        if did in stopped:
            mark = "⏹"
        elif did in paused:
            mark = "⏸"
        elif did in kicked:
            mark = "✅"
        else:
            mark = "🟢"
        info = run.results.get(did, {})
        acct = info.get("acct")
        name = info.get("name")
        acct_str = str(acct) if acct is not None else "—"
        name_str = name if name else ("belum dicek" if did not in kicked else "?")
        lines.append(f"{mark} {i + 1}. {acct_str} | {name_str}")
        lines.append(f"   <code>{html.escape(did)}</code>")
    lines.append(f"<code>{BAR}</code>")

    rows = []
    for i, did in enumerate(chunk, start=start):
        if did in stopped:
            mark = "⏹"
        elif did in paused:
            mark = "⏸"
        elif did in kicked:
            mark = "✅"
        else:
            mark = "🟢"
        info = run.results.get(did, {})
        acct = info.get("acct")
        name = info.get("name")
        acct_str = str(acct) if acct is not None else "—"
        name_str = name if name else ("belum dicek" if did not in kicked else "?")
        label = f"{mark} {i + 1}. {acct_str} · {name_str[:14]}"
        rows.append([{"text": label, "callback_data": f"tog:{run.run_id}:{i}"}])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append({"text": "« Prev", "callback_data": f"dev:{run.run_id}:{page-1}"})
        nav.append({"text": f"{page+1}/{pages}", "callback_data": "noop"})
        if page < pages - 1:
            nav.append({"text": "Next »", "callback_data": f"dev:{run.run_id}:{page+1}"})
        rows.append(nav)
    rows.append([{"text": "📊 Status", "callback_data": f"st:{run.run_id}"},
                 {"text": "🏠 Menu", "callback_data": "menu"}])
    # Tombol navigasi ke run lain (biar user nggak perlu scroll ke atas)
    nav_rows = run_nav_buttons(active_runs, run.run_id)
    rows.extend(nav_rows)
    return "\n".join(lines), rows


# ── device list view ──────────────────────────────────────────────────────
def device_list_view(devices, page=0, budget=3000):
    """Full device ids, one per line, paginated by character budget."""
    lines_all = [f"{i}. <code>{did}</code>" for i, did in enumerate(devices, 1)]
    pages, idx = [], 0
    while idx < len(lines_all) or not pages:
        chunk, used = [], 0
        while idx < len(lines_all):
            ln = len(lines_all[idx]) + 1
            if chunk and used + ln > budget:
                break
            chunk.append(idx)
            used += ln
            idx += 1
        if not chunk:
            chunk = [idx]
            idx += 1
        pages.append(chunk)

    page = max(0, min(page, len(pages) - 1))
    total = len(devices)
    out = [f"📋 <b>Device List</b> (full)",
           f"<code>{BAR}</code>",
           f"Total <b>{total}</b> · hal {page + 1}/{len(pages)}",
           "Tap baris buat salin ID-nya.", ""]
    if not devices:
        out.append("(kosong — kirim device id atau upload .txt)")
    for i in pages[page]:
        out.append(lines_all[i])

    kb_rows = []
    nav = []
    if len(pages) > 1:
        if page > 0:
            nav.append({"text": "« Prev", "callback_data": f"list:{page-1}"})
        nav.append({"text": f"{page+1}/{len(pages)}", "callback_data": "noop"})
        if page < len(pages) - 1:
            nav.append({"text": "Next »", "callback_data": f"list:{page+1}"})
    if nav:
        kb_rows.append(nav)
    kb_rows.append([{"text": "🏠 Menu", "callback_data": "menu"}])
    return "\n".join(out), kb_rows


# ── info ──────────────────────────────────────────────────────────────────
def info_view():
    text = (
        "ℹ️ <b>KickLur</b>\n"
        f"<code>{BAR}</code>\n"
        "Port setia dari <code>brutetolslhoya</code>.\n\n"
        f"Login  : <code>{cfg.KICK_HOST}:{cfg.KICK_PORT}</code>\n"
        f"CLI    : <code>{cfg.KICK_CLI_VERSION}</code>\n"
        f"Channel: <code>{cfg.KICK_CHANNEL}</code>\n\n"
        f"Kick timeout : <b>{cfg.KICK_TIMEOUT}s</b>\n"
        f"Worker/run   : <b>{cfg.BF_WORKERS}</b>\n"
        f"Maks total   : <b>{cfg.BF_MAX_CONCURRENCY}</b>\n"
        f"Jeda default : <b>{cfg.BF_KICK_DELAY}s</b>\n"
        f"Lookup nama  : <b>{'on' if cfg.BF_LOOKUP else 'off'}</b>\n\n"
        "<b>Arti tanda</b>\n"
        "🟢 jalan · ⏸ pause · ✅ pernah sukses · ⏹ dihapus"
    )
    return text, [[{"text": "🏠 Menu", "callback_data": "menu"}]]


def help_text():
    return (
        "⚡ <b>KICKLUR</b>\n"
        f"<code>{BAR}</code>\n"
        "<b>Cara pakai</b>\n"
        "1. Tambah device (kirim id, atau upload .txt)\n"
        "2. START KICK\n"
        "3. Buka 📋 Device buat pause/lanjut per device\n\n"
        "<b>Perintah</b>\n"
        "/start — menu\n"
        "/menu — menu\n"
        "/status — status run\n"
        "/stop — stop semua\n"
        "/reset — kosongkan list\n"
        "/help — ini"
    )
