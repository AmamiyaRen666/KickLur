"""Menus, keyboards and message bodies. Pure rendering — no I/O."""
import html
import math
import time

PAGE_SIZE = 8

BANNER = "💥 <b>KICKLUR</b> · BF Kicker"


def _short(did, n=22):
    return did[:n] + "…" if len(did) > n else did


def home_text(device_count, active_runs):
    if device_count:
        tail = ("Sending more ids <b>adds</b> to the list — it never replaces it.\n"
                "Use 🗑 Reset to start over.")
    else:
        tail = ("Send device ids as text (space/comma separated) or upload a .txt.\n"
                "Send them in batches too — the list accumulates.")
    runs = f"🟢 Run aktif: <b>{active_runs}</b>\n" if active_runs else ""
    return (f"{BANNER}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📱 Device di list: <b>{device_count}</b>\n"
            f"{runs}\n"
            f"{tail}")


def home_kb(device_count, active_runs):
    rows = []
    if device_count:
        rows.append([{"text": f"▶️ Mulai Kick ({device_count} device)",
                      "callback_data": "start"}])
    if device_count:
        rows.append([{"text": "🗑 Reset List", "callback_data": "reset"}])
    rows.append([{"text": "🏠 Menu", "callback_data": "menu"}])
    return rows


def _bar(done, total, w=20):
    if not total:
        return ""
    pct = min(100, int(done / total * 100))
    fill = int(w * pct / 100)
    return f"<code>{'█' * fill}{'░' * (w - fill)}</code> {pct}%\n"


def _results_block(snap, limit=8):
    rows = snap.get("results") or []
    if not rows:
        return ""
    lines = []
    for dev, info in rows[:limit]:
        mark = "✅" if info.get("ok") else "❌"
        bits = []
        acct = info.get("acct")
        if acct is not None:
            bits.append(f"<code>{html.escape(str(acct))}</code>")
        name = info.get("name")
        if name:
            bits.append(f"「{html.escape(str(name))}」")
        if not bits:
            bits.append(f"<code>{_short(dev, 18)}</code>")
        lines.append(f"  {mark} {' · '.join(bits)}")
    more = f"\n  … +{len(rows) - limit} lagi" if len(rows) > limit else ""
    return "📋 <b>Hasil:</b>\n" + "\n".join(lines) + more + "\n"


def run_text(snap):
    loops = "∞" if snap["loops"] == 0 else snap["loops"]
    bar = _bar(snap["kicks"], snap["loops"])
    results = _results_block(snap)
    note = f"{snap['note']}\n" if snap["note"] else ""
    err = f"\n{snap['last_err']}" if snap["last_err"] else ""
    return (f"💥 <b>RUN #{snap['run_id']}</b> · {snap['devices']} device\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{bar}"
            f"🔁 Kick     : <b>{snap['kicks']}</b> / {loops}\n"
            f"✅ Sukses    : <b>{snap['ok']}</b>\n"
            f"❌ Gagal     : <b>{snap['fail']}</b>\n"
            f"📱 Device jalan: <b>{snap['active']}</b> / {snap['devices']}\n"
            f"⏱ Elapsed   : <b>{int(snap['elapsed'])}s</b>\n"
            f"{note}\n"
            f"{results}"
            f"━━━━━━━━━━━━━━━\n"
            f"<i>📊 Status = refresh · 🗑 Hapus = kelola device</i>{err}")


def run_kb(run_id):
    return [[{"text": "📊 Status", "callback_data": f"status:{run_id}"},
             {"text": "🗑 Hapus", "callback_data": f"hapus:{run_id}"}],
            [{"text": "🏠 Menu", "callback_data": f"status:{run_id}"}]]


MENU_KB = [[{"text": "🏠 Menu", "callback_data": "menu"}]]


def run_finished_text(snap, reason):
    loops = "∞" if snap["loops"] == 0 else snap["loops"]
    head = {"done": "✅", "stopped": "⛔"}.get(reason, "💥")
    label = {"done": "SELESAI", "stopped": "STOPPED", "empty": "KOSONG"}.get(reason, reason.upper())
    others = snap.get("others", 0)
    others_line = f"\n🟢 Run lain masih jalan: <b>{others}</b>" if others else ""
    err = f"\n\n{snap['last_err']}" if snap["last_err"] else ""
    paused_line = (f"\n⏸ Device di-pause: <b>{snap['paused']}</b>"
                   if snap.get("paused") else "")
    results = _results_block(snap)
    return (f"{head} <b>RUN #{snap['run_id']} {label}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🔁 Kick     : <b>{snap['kicks']}</b> / {loops}\n"
            f"✅ Sukses    : <b>{snap['ok']}</b>\n"
            f"❌ Gagal     : <b>{snap['fail']}</b>\n"
            f"📱 Device   : <b>{snap['devices']}</b>"
            f"{others_line}{paused_line}{err}\n\n"
            f"{results}")


def hapus_text(run, page=0):
    """Device panel text: full device id + account id + nickname."""
    devices = run.devices
    stopped = run.stopped_set()
    paused = run.paused_set()
    kicked = run.kicked_set()
    pages = max(1, math.ceil(len(devices) / PAGE_SIZE))
    page = max(0, min(int(page), pages - 1))
    start = page * PAGE_SIZE
    chunk = list(enumerate(devices))[start:start + PAGE_SIZE]

    n_run = len(devices) - len(stopped) - len(paused)
    lines = [
        f"🗑 <b>DEVICE — RUN #{run.run_id}</b>",
        "━━━━━━━━━━━━━━━",
        f"🟢 Jalan <b>{n_run}</b> · ⏸ Pause <b>{len(paused)}</b> · "
        f"⏹ Hapus <b>{len(stopped)}</b> · Total <b>{len(devices)}</b>",
        f"hal {page + 1}/{pages} · <b>klik = pause/lanjut</b>",
        "",
    ]
    for i, did in chunk:
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
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def hapus_kb(run, page=0):
    """Device panel keyboard: one button per device, nav, hapus semua."""
    devices = run.devices
    stopped = run.stopped_set()
    paused = run.paused_set()
    kicked = run.kicked_set()
    pages = max(1, math.ceil(len(devices) / PAGE_SIZE))
    page = max(0, min(int(page), pages - 1))
    start = page * PAGE_SIZE
    rows = []
    for i, did in list(enumerate(devices))[start:start + PAGE_SIZE]:
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
        rows.append([{"text": f"{mark} {i + 1}. {acct_str} · {name_str[:14]}",
                      "callback_data": f"toggle:{run.run_id}:{i}"}])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append({"text": "⬅️",
                        "callback_data": f"hapuspage:{run.run_id}:{page - 1}"})
        nav.append({"text": f"{page + 1}/{pages}", "callback_data": "noop"})
        if page < pages - 1:
            nav.append({"text": "➡️",
                        "callback_data": f"hapuspage:{run.run_id}:{page + 1}"})
        rows.append(nav)
    rows.append([{"text": "🗑 Hapus Semua", "callback_data": f"hapusall:{run.run_id}"}])
    rows.append([{"text": "📊 Status", "callback_data": f"status:{run.run_id}"},
                 {"text": "🏠 Menu", "callback_data": "menu"}])
    return rows
