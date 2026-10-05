"""Run registry + worker pool.

One run per user. Each run owns:
  - a worker pool (BF_WORKERS threads) so a slow device never idles the rest
  - per-device pause/resume and a permanent stop marker
  - a global concurrency budget shared across all runs (BF_MAX_CONCURRENCY)

Pause is authoritative. A device carries an `in_flight` flag that is claimed
and released under the run lock, so:

  * two workers can never kick the same device at the same time
  * once a device is PAUSED, no new kick starts on it — including from workers
    that had already picked it and were waiting for a free slot

A kick already on the wire finishes on its own; a socket call cannot be
aborted. The panel says so instead of pretending otherwise.

"Loop" matches the source: 1 loop = 1 kick, devices rotate.
"""
import threading
import time
from dataclasses import dataclass, field

from . import config as cfg
from .engine import cached_name, kick_once

RUNNING = "running"
PAUSED = "paused"
STOPPED = "stopped"
DONE = "done"


@dataclass
class DeviceState:
    device_id: str
    kicks: int = 0
    ok: int = 0
    fail: int = 0
    last_ms: float = 0.0
    last_desc: str = ""
    nick: str = ""
    acc: object = None
    state: str = RUNNING          # running | paused | stopped
    ever_ok: bool = False
    in_flight: bool = False       # guarded by Run.lock

    @property
    def mark(self):
        if self.state == STOPPED:
            return "⏹"
        if self.state == PAUSED:
            return "⏸"
        if self.ever_ok:
            return "✅"
        return "🟢"


@dataclass
class Run:
    run_id: int
    chat_id: int
    message_id: int
    total_loops: int              # 0 = unlimited
    delay_sec: float
    devices: dict = field(default_factory=dict)   # device_id -> DeviceState
    order: list = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    status: str = RUNNING
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    kicks: int = 0
    ok: int = 0
    fail: int = 0
    latencies: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    workers_alive: int = 0
    # What the run's message currently shows. The background updater only
    # touches the message while it is still showing "status"; once the user
    # opens the device panel the updater leaves it alone, so a message never
    # changes under the user's hands.
    view: str = "status"
    edit_lock: threading.Lock = field(default_factory=threading.Lock)
    last_panel_text: str = ""

    @property
    def elapsed(self):
        return (self.finished or time.time()) - self.started

    @property
    def speed(self):
        el = self.elapsed
        return self.kicks / el if el > 0 else 0.0

    @property
    def avg_ms(self):
        return (sum(self.latencies) / len(self.latencies)) if self.latencies else 0.0

    def live_devices(self):
        """Devices that are not permanently stopped."""
        return [d for d in self.devices.values() if d.state != STOPPED]

    def running_devices(self):
        return [d for d in self.devices.values() if d.state == RUNNING]

    def paused_devices(self):
        return [d for d in self.devices.values() if d.state == PAUSED]

    @property
    def all_paused(self):
        live = self.live_devices()
        return bool(live) and not any(d.state == RUNNING for d in live)

    def progress(self):
        """(done, total) for the progress line."""
        return self.kicks, (self.total_loops or 0)


class RunRegistry:
    def __init__(self):
        self._runs = {}
        self._next_id = 1
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(cfg.BF_MAX_CONCURRENCY)

    # ── lifecycle ──────────────────────────────────────────────────────────
    def create(self, chat_id, message_id, device_ids, total_loops, delay_sec):
        with self._lock:
            run_id = self._next_id
            self._next_id += 1
            run = Run(run_id=run_id, chat_id=chat_id, message_id=message_id,
                      total_loops=total_loops, delay_sec=delay_sec)
            for did in device_ids:
                d = DeviceState(device_id=did)
                # Seed from the name cache so a device already resolved in an
                # earlier run shows its name immediately, instead of "?" until
                # the first kick lands.
                c_acc, c_nick = cached_name(device_id=did)
                if c_nick:
                    d.nick = c_nick
                    if c_acc:
                        d.acc = c_acc
                run.devices[did] = d
                run.order.append(did)
            self._runs[run_id] = run
            return run

    def get(self, run_id):
        with self._lock:
            return self._runs.get(run_id)

    def all_runs(self):
        with self._lock:
            return list(self._runs.values())

    def active_runs(self, chat_id=None):
        return [r for r in self.all_runs()
                if r.status in (RUNNING, PAUSED)
                and (chat_id is None or r.chat_id == chat_id)]

    def stop(self, run_id):
        run = self.get(run_id)
        if not run:
            return False
        run.stop_event.set()
        with run.lock:
            run.status = STOPPED
            for d in run.devices.values():
                d.state = STOPPED
        return True

    def stop_all(self, chat_id=None):
        n = 0
        for r in self.active_runs(chat_id):
            if self.stop(r.run_id):
                n += 1
        return n

    def toggle_device(self, run_id, device_id):
        """Pause <-> resume one device. Returns the new state, or None.

        Takes the run lock, so it cannot interleave with _claim: when this
        returns, the device is either PAUSED (no new kick will start) or
        RUNNING.
        """
        run = self.get(run_id)
        if not run:
            return None
        with run.lock:
            d = run.devices.get(device_id)
            if d is None:
                return None
            if d.state == STOPPED:
                return d.state
            d.state = PAUSED if d.state == RUNNING else RUNNING
            return d.state

    def set_all_paused(self, run_id, paused):
        """Pause/resume every live device in one go. Returns how many changed."""
        run = self.get(run_id)
        if not run:
            return 0
        n = 0
        with run.lock:
            for d in run.devices.values():
                if d.state == STOPPED:
                    continue
                want = PAUSED if paused else RUNNING
                if d.state != want:
                    d.state = want
                    n += 1
        return n

    def resume_all(self, run_id):
        return self.set_all_paused(run_id, False)

    # ── execution ──────────────────────────────────────────────────────────
    def start(self, run, want_lookup=True):
        run.status = RUNNING
        n = min(cfg.BF_WORKERS, max(1, len(run.order)))
        run.workers_alive = n
        for i in range(n):
            threading.Thread(target=self._worker, args=(run, i, want_lookup),
                             name=f"kicklur-{run.run_id}-{i}", daemon=True).start()

    def _claim(self, run, cursor):
        """Atomically claim the next kickable device.

        Kickable = RUNNING and not already in flight. Claiming happens under
        the run lock, so the same device can never be handed to two workers.
        """
        with run.lock:
            n = len(run.order)
            if n == 0:
                return None, None, cursor
            for _ in range(n):
                cursor = (cursor + 1) % n
                did = run.order[cursor]
                d = run.devices[did]
                if d.state == RUNNING and not d.in_flight:
                    d.in_flight = True
                    return did, d, cursor
        return None, None, cursor

    def _worker(self, run, idx, want_lookup):
        cursor = idx - 1
        try:
            while not run.stop_event.is_set():
                with run.lock:
                    if run.total_loops and run.kicks >= run.total_loops:
                        break

                did, d, cursor = self._claim(run, cursor)
                if did is None:
                    # nothing kickable: either all devices are paused, or all
                    # live ones are busy in other workers
                    if not run.live_devices():
                        break
                    time.sleep(0.1)
                    continue

                try:
                    self._slots.acquire()
                    try:
                        if run.stop_event.is_set():
                            break
                        # Re-check under the lock. The user may have paused this
                        # device, or another worker may have consumed the last
                        # loop, while we were queued for a slot.
                        with run.lock:
                            if d.state != RUNNING:
                                continue
                            if run.total_loops and run.kicks >= run.total_loops:
                                break
                            # Reserve the loop BEFORE kicking, otherwise several
                            # workers pass the budget test at once.
                            run.kicks += 1

                        ok, msg, profile = kick_once(did, want_lookup=want_lookup)

                        with run.lock:
                            d.kicks += 1
                            if ok:
                                run.ok += 1
                                d.ok += 1
                                d.ever_ok = True
                            else:
                                run.fail += 1
                                d.fail += 1
                            d.last_desc = msg
                            ms = _parse_ms(msg)
                            if ms is not None:
                                d.last_ms = ms
                                run.latencies.append(ms)
                            if profile:
                                d.nick = profile.get("nick") or d.nick
                                d.acc = profile.get("acc") or d.acc
                    finally:
                        self._slots.release()
                finally:
                    with run.lock:
                        d.in_flight = False

                if run.delay_sec > 0 and not run.stop_event.is_set():
                    slept = 0.0
                    while slept < run.delay_sec and not run.stop_event.is_set():
                        step = min(0.1, run.delay_sec - slept)
                        time.sleep(step)
                        slept += step
        finally:
            with run.lock:
                run.workers_alive -= 1
                last = run.workers_alive <= 0
            if last:
                run.finished = time.time()
                if run.status != STOPPED:
                    run.status = DONE


def _parse_ms(msg):
    """kick_once reports '… · 5ms · acc · nick'; pull the ms back out."""
    try:
        part = msg.split("·")[1].strip()
        if part.endswith("ms"):
            return float(part[:-2].strip())
    except Exception:
        pass
    return None


REGISTRY = RunRegistry()
