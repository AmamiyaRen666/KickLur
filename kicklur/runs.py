"""Run registry + worker pool.

One run per user. Each run owns:
  - a worker pool (BF_WORKERS threads), so a slow device never idles the rest
  - per-device pause/resume (temporary) and a permanent stop marker
  - a global concurrency budget shared across all runs (BF_MAX_CONCURRENCY)

Semantics of "loop" match the source: 1 loop = 1 kick, devices rotate.
"""
import threading
import time
from dataclasses import dataclass, field

from . import config as cfg
from .engine import kick_once

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
    last_edit: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    workers_alive: int = 0
    finished_notified: bool = False

    @property
    def speed(self):
        el = (self.finished or time.time()) - self.started
        return self.kicks / el if el > 0 else 0.0

    @property
    def avg_ms(self):
        return (sum(self.latencies) / len(self.latencies)) if self.latencies else 0.0

    def live_devices(self):
        """Devices that can still be kicked."""
        return [d for d in self.devices.values() if d.state != STOPPED]

    def all_paused(self):
        live = self.live_devices()
        return bool(live) and all(d.state == PAUSED for d in live)


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
                run.devices[did] = DeviceState(device_id=did)
                run.order.append(did)
            self._runs[run_id] = run
            return run

    def get(self, run_id):
        with self._lock:
            return self._runs.get(run_id)

    def all_runs(self):
        with self._lock:
            return list(self._runs.values())

    def active_runs(self):
        return [r for r in self.all_runs() if r.status in (RUNNING, PAUSED)]

    def stop(self, run_id):
        run = self.get(run_id)
        if not run:
            return False
        run.stop_event.set()
        run.status = STOPPED
        for d in run.devices.values():
            d.state = STOPPED
        return True

    def stop_all(self, chat_id=None):
        n = 0
        for r in self.active_runs():
            if chat_id is not None and r.chat_id != chat_id:
                continue
            if self.stop(r.run_id):
                n += 1
        return n

    def toggle_device(self, run_id, device_id):
        run = self.get(run_id)
        if not run or device_id not in run.devices:
            return None
        d = run.devices[device_id]
        if d.state == STOPPED:
            return d.state
        d.state = PAUSED if d.state == RUNNING else RUNNING
        return d.state

    # ── execution ──────────────────────────────────────────────────────────
    def start(self, run, want_lookup=True):
        """Launch the worker pool. Returns immediately."""
        run.status = RUNNING
        n = min(cfg.BF_WORKERS, max(1, len(run.order)))
        run.workers_alive = n
        for i in range(n):
            t = threading.Thread(target=self._worker, args=(run, i, want_lookup),
                                 name=f"kicklur-{run.run_id}-{i}", daemon=True)
            t.start()

    def _pick_next(self, run, cursor):
        """Round-robin over devices that are running (not paused/stopped)."""
        n = len(run.order)
        if n == 0:
            return None
        for _ in range(n):
            cursor = (cursor + 1) % n
            did = run.order[cursor]
            d = run.devices[did]
            if d.state == RUNNING:
                return did
        return None

    def _worker(self, run, idx, want_lookup):
        cursor = idx - 1
        try:
            while not run.stop_event.is_set():
                if run.total_loops and run.kicks >= run.total_loops:
                    break
                did = self._pick_next(run, cursor)
                if did is None:
                    # everything paused or stopped
                    if run.live_devices() == []:
                        break
                    time.sleep(0.2)
                    continue
                cursor = run.order.index(did)

                # global budget across all runs
                self._slots.acquire()
                try:
                    if run.stop_event.is_set():
                        break
                    d = run.devices[did]
                    if d.state != RUNNING:
                        continue
                    # Reserve the loop slot BEFORE kicking. Checking the budget
                    # after the kick let several workers pass the test at once
                    # (3 devices x 3 loops produced 4 kicks), because the
                    # counter only moved once the network call returned.
                    with run.lock:
                        if run.total_loops and run.kicks >= run.total_loops:
                            break
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
                        # latency: kick_once does not expose it, so parse it
                        # back out of the message ("… · 5ms · acc · nick")
                        try:
                            part = msg.split("·")[1].strip()
                            if part.endswith("ms"):
                                ms = float(part[:-2].strip())
                                d.last_ms = ms
                                run.latencies.append(ms)
                        except Exception:
                            pass
                        if profile:
                            d.nick = profile.get("nick") or d.nick
                            d.acc = profile.get("acc") or d.acc
                finally:
                    self._slots.release()

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


REGISTRY = RunRegistry()
