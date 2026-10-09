"""Independent, concurrently running kick jobs.

Runs live in their own registry instead of sharing one job slot, so starting a
new run never stops the one already in flight, and stopping one run leaves every
other run alone.
"""
import collections
import html
import math
import threading
import time

from . import config as cfg
from .kicker import kick_once

# Bounds the total number of in-flight kicks across ALL runs, with a per-run
# quota so no run can starve another.
_ACTIVE_RUNS = [0]
_ACTIVE_RUNS_LOCK = threading.Lock()


def _register_run(delta):
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS[0] = max(0, _ACTIVE_RUNS[0] + delta)
        return _ACTIVE_RUNS[0]


def _active_run_count():
    with _ACTIVE_RUNS_LOCK:
        return max(1, _ACTIVE_RUNS[0])


def _quota_for(workers_cap):
    """Slots one run may hold right now (>= 1, so it always makes progress)."""
    return max(1, min(workers_cap, cfg.BF_MAX_CONCURRENCY // _active_run_count()))


class SlotPool:
    """Global kick-slot pool with a live per-run quota."""

    def __init__(self, capacity):
        self.capacity = max(1, capacity)
        self.cond = threading.Condition()
        self.used = 0
        self.per_run = {}

    def acquire(self, run_key, workers_cap, timeout=30.0):
        deadline = time.time() + timeout
        with self.cond:
            while True:
                quota = _quota_for(workers_cap)
                held = self.per_run.get(run_key, 0)
                if self.used < self.capacity and held < quota:
                    self.used += 1
                    self.per_run[run_key] = held + 1
                    return True
                if time.time() > deadline:
                    return False
                self.cond.wait(0.2)

    def release(self, run_key):
        with self.cond:
            held = self.per_run.get(run_key, 0)
            if held > 0:
                self.per_run[run_key] = held - 1
                self.used = max(0, self.used - 1)
            self.cond.notify_all()

    def drop_run(self, run_key):
        with self.cond:
            self.per_run.pop(run_key, None)
            self.cond.notify_all()


_SLOTS = SlotPool(cfg.BF_MAX_CONCURRENCY)

_WAIT = object()


class Run:
    def __init__(self, run_id, chat_id, devices):
        self.run_id = run_id
        self.chat_id = str(chat_id)
        self.devices = list(devices)
        self.stopped_devices = set()
        self.paused_devices = set()
        self.stop = False
        self.finished = False
        self.start = time.time()
        self.lock = threading.Lock()
        self.kicks = 0
        self.ok = 0
        self.fail = 0
        self.passes = 0
        self.current = ""
        self.last_err = ""
        self.note = ""
        self.loops = 0          # 0 means unlimited
        self.message_id = None
        self.results = {}
        self.recent = collections.deque(maxlen=15)

    @property
    def alive(self):
        return not self.stop and not self.finished

    def request_stop(self):
        with self.lock:
            self.stop = True

    def stop_device(self, device):
        """Permanent: the device never runs again in this run."""
        with self.lock:
            self.stopped_devices.add(device)
            self.paused_devices.discard(device)

    def pause_device(self, device):
        """Temporary: skipped while paused, runs again after resume."""
        with self.lock:
            if device in self.stopped_devices:
                return False
            self.paused_devices.add(device)
            return True

    def resume_device(self, device):
        with self.lock:
            self.paused_devices.discard(device)
            return True

    def is_stopped(self, device):
        with self.lock:
            return device in self.stopped_devices

    def is_paused(self, device):
        with self.lock:
            return device in self.paused_devices

    def toggle_pause(self, device):
        """Pause a running device, or resume a paused one. Returns the new state."""
        with self.lock:
            if device in self.stopped_devices:
                return "stopped"
            if device in self.paused_devices:
                self.paused_devices.discard(device)
                return "running"
            self.paused_devices.add(device)
            return "paused"

    def pending(self):
        """Devices that can still be kicked right now."""
        with self.lock:
            return [d for d in self.devices
                    if d not in self.stopped_devices
                    and d not in self.paused_devices]

    runnable = pending

    def stopped_set(self):
        with self.lock:
            return set(self.stopped_devices)

    def paused_set(self):
        with self.lock:
            return set(self.paused_devices)

    def has_paused(self):
        with self.lock:
            return bool(self.paused_devices)

    def active_count(self):
        return len(self.pending())

    def kicked_set(self):
        with self.lock:
            return set(self.results.keys())

    def set_passes(self, n):
        with self.lock:
            self.passes = n

    def record(self, device, ok, msg, profile=None):
        with self.lock:
            self.kicks += 1
            self.current = device
            info = self.results.get(device) or {}
            info["ok"] = ok
            if profile:
                acct = profile.get("account_id")
                if acct is not None:
                    info["acct"] = acct
                if profile.get("name"):
                    info["name"] = profile["name"]
            self.results[device] = info
            try:
                self.recent.remove(device)
            except ValueError:
                pass
            self.recent.append(device)
            if ok:
                self.ok += 1
            else:
                self.fail += 1
                if not self.last_err:
                    self.last_err = msg

    def snapshot(self):
        with self.lock:
            return {
                "run_id": self.run_id,
                "devices": len(self.devices),
                "active": len([d for d in self.devices
                               if d not in self.stopped_devices
                               and d not in self.paused_devices]),
                "stopped": len(self.stopped_devices),
                "paused": len(self.paused_devices),
                "kicks": self.kicks,
                "ok": self.ok,
                "fail": self.fail,
                "passes": self.passes,
                "current": self.current,
                "last_err": self.last_err,
                "note": self.note,
                "loops": self.loops,
                "start": self.start,
                "elapsed": time.time() - self.start,
                "results": [(d, dict(self.results.get(d) or {}))
                            for d in reversed(self.recent)],
            }


class RunRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0
        self._runs = {}

    def create(self, chat_id, devices):
        with self._lock:
            self._seq += 1
            run = Run(self._seq, chat_id, devices)
            self._runs[(str(chat_id), self._seq)] = run
            return run

    def get(self, chat_id, run_id):
        try:
            run_id = int(run_id)
        except (TypeError, ValueError):
            return None
        with self._lock:
            return self._runs.get((str(chat_id), run_id))

    def list(self, chat_id):
        with self._lock:
            runs = [r for k, r in self._runs.items() if k[0] == str(chat_id)]
        return sorted(runs, key=lambda r: r.run_id)

    def active(self, chat_id):
        return [r for r in self.list(chat_id) if r.alive]

    def stop(self, chat_id, run_id):
        run = self.get(chat_id, run_id)
        if not run:
            return False
        run.request_stop()
        return True

    def stop_all(self, chat_id):
        n = 0
        for run in self.active(chat_id):
            run.request_stop()
            n += 1
        return n

    def end(self, run):
        with self._lock:
            self._runs.pop((run.chat_id, run.run_id), None)
        with run.lock:
            run.finished = True


class RunExecutor(threading.Thread):
    """Drives one Run with a continuous worker pool.

    `loops` is a KICK budget. math.inf means unlimited.
    """

    def __init__(self, run, loops, registry, on_finish=None):
        super().__init__(daemon=True)
        self.job = run
        self.loops = loops
        self.registry = registry
        self.on_finish = on_finish
        # FIX: store math.inf as 0 (unlimited) in run.loops for display,
        # but keep the real budget in self.loops
        self.job.loops = 0 if loops == math.inf else int(loops)
        self.workers_cap = max(1, min(cfg.BF_WORKERS, cfg.BF_MAX_CONCURRENCY))
        self.workers = self.workers_cap
        self.kick_delay = cfg.BF_KICK_DELAY
        self._claim_lock = threading.Lock()
        self._cursor = 0
        self._claimed = 0
        self._in_flight = set()
        self._device_next = {}
        self._wait_hint = 0.05
        self._stop_event = threading.Event()

    def _next_device(self):
        with self._claim_lock:
            if not self.job.alive:
                return None, None
            # FIX: only check budget if loops is not infinite
            if self.loops != math.inf and self._claimed >= self.loops:
                return None, None
            n = len(self.job.devices)
            if n == 0:
                return None, None
            cooling = False
            for _ in range(n):
                dev = self.job.devices[self._cursor]
                self._cursor = (self._cursor + 1) % n
                if self.job.is_stopped(dev) or self.job.is_paused(dev):
                    continue
                if dev in self._in_flight:
                    continue
                if self._device_next.get(dev, 0.0) > time.time():
                    cooling = True
                    continue
                self._claimed += 1
                self._in_flight.add(dev)
                self.job.set_passes(self._claimed // n)
                return dev, None
            if self._in_flight or cooling:
                soonest = min([self._device_next.get(d, 0.0)
                               for d in self.job.devices] or [0.0])
                hint = soonest - time.time() if soonest > time.time() else 0.05
                self._wait_hint = max(0.02, min(hint, 0.25))
                return None, _WAIT
            if self.job.has_paused():
                self.job.note = "semua device di-pause"
                return None, _WAIT
            if self.job.stopped_set():
                self.job.note = "semua device dihapus"
            return None, None

    def _release_device(self, device):
        with self._claim_lock:
            self._in_flight.discard(device)

    def _refund(self):
        with self._claim_lock:
            if self._claimed > 0:
                self._claimed -= 1

    def _kick(self, device):
        # Cek paused SEBELUM acquire slot — kalau paused, jangan acquire
        # slot sama sekali. Ini bikin pause langsung efek tanpa nunggu slot.
        if not self.job.alive or self.job.is_stopped(device):
            return None
        if self.job.is_paused(device):
            return None
        got = _SLOTS.acquire(self.job.run_id, self.workers_cap)
        if not got:
            return None
        try:
            # Re-check setelah acquire — device bisa di-pause saat nunggu slot
            if not self.job.alive or self.job.is_stopped(device):
                return None
            if self.job.is_paused(device):
                return None
            try:
                ok, msg, profile = kick_once(device)
            except Exception as e:
                ok, msg, profile = False, f"❌ {type(e).__name__}: {e}", None
            return ok, msg, profile
        finally:
            _SLOTS.release(self.job.run_id)

    def _worker(self):
        while True:
            device, sentinel = self._next_device()
            if device is None:
                if sentinel is _WAIT:
                    if self._stop_event.wait(getattr(self, "_wait_hint", 0.05)):
                        return
                    continue
                return
            result = self._kick(device)
            self._release_device(device)
            if result is None:
                self._refund()
                continue
            ok, msg, profile = result
            self.job.record(device, ok, msg, profile)
            if self.kick_delay > 0:
                with self._claim_lock:
                    self._device_next[device] = time.time() + self.kick_delay

    def run(self):
        _register_run(+1)
        try:
            threads = []
            for _ in range(self.workers):
                t = threading.Thread(target=self._worker, daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
        finally:
            self._stop_event.set()
            _register_run(-1)
            _SLOTS.drop_run(self.job.run_id)
            self.registry.end(self.job)
            if self.on_finish:
                try:
                    self.on_finish(self.job)
                except Exception:
                    pass


# ── UI helpers ────────────────────────────────────────────────────────────
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
            bits.append(f"<code>{dev[:18]}</code>")
        lines.append(f"  {mark} {' · '.join(bits)}")
    more = f"\n  … +{len(rows) - limit} lagi" if len(rows) > limit else ""
    return "📋 <b>Hasil:</b>\n" + "\n".join(lines) + more + "\n"
