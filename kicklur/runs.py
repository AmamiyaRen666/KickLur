"""Independent, concurrently running kick jobs.

Runs live in their own registry instead of sharing one job slot, so starting a
new run never stops the one already in flight, and stopping one run leaves every
other run alone.
"""
import collections
import math
import threading
import time

from . import config as cfg
from .kicker import kick_once

# Bounds the total number of in-flight kicks across ALL runs, with a per-run
# quota so no run can starve another.
#
# A plain shared semaphore was not enough: the oldest run's workers released a
# slot and grabbed it straight back, so a newer run never won one and sat at
# "Kick: 0" forever (reproduced: run #1 at 18 kicks, run #2 at 0).
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
    """Global kick-slot pool with a live per-run quota.

    A run may only hold up to its quota, recomputed against the number of live
    runs. Quotas shrink as runs are added, so an old run drains its excess on
    the next release and the new run gets its turn.
    """

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
        """Forget a finished run's bookkeeping and wake any waiters."""
        with self.cond:
            self.per_run.pop(run_key, None)
            self.cond.notify_all()


_SLOTS = SlotPool(cfg.BF_MAX_CONCURRENCY)

# Sentinel from the claim step: nothing is kickable *right now*, but the run is
# still alive (every remaining device is paused). Waiting is correct; ending
# the run would throw away a pause that the user means to resume.
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
        # device -> {"ok", "acct", "name"} plus the order devices were touched,
        # so the status message can list account ids and nicknames.
        self.results = {}
        self.recent = collections.deque(maxlen=15)

    # ── liveness ───────────────────────────────────────────────────────────
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

    # `runnable` is the name the executor uses; kept separate from `pending`
    # only for readability at the call sites.
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

    # ── accounting ─────────────────────────────────────────────────────────
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

    `loops` is a KICK budget (same meaning as the original implementation).
    Workers pull the next kickable device the moment they free up — there is no
    batch barrier, so one slow device (a 15s timeout) never parks the others,
    and a device that is paused or stopped is skipped on the very next pull
    instead of at the end of a batch.
    """

    def __init__(self, run, loops, registry, on_finish=None):
        super().__init__(daemon=True)
        # NOTE: the Run is stored as `job`, never as `self.run` — Thread defines
        # run() as the entry point, and an attribute of that name shadows it
        # with a non-callable, which makes the thread die on bootstrap.
        self.job = run
        self.loops = loops
        self.registry = registry
        self.on_finish = on_finish
        self.job.loops = 0 if loops == math.inf else int(loops)
        # Ceiling for this run. The live quota is decided per kick by the slot
        # pool, against the number of runs that are alive right now.
        self.workers_cap = max(1, min(cfg.BF_WORKERS, cfg.BF_MAX_CONCURRENCY))
        self.workers = self.workers_cap
        # Pause between kicks per worker. The original BF Kicker waited
        # BF_KICK_DELAY (0.5s) after every kick plus a 0.01s login rate limit,
        # and that pacing is what keeps the login/game server from treating the
        # device as a bot. Sending with no gap produced "Login server rejected
        # the device id" and "GAME SERVER REFUSED".
        self.kick_delay = cfg.BF_KICK_DELAY
        # Shared pull state. The lock guards cursor + claimed so two workers can
        # never take the same device or overshoot the budget. _in_flight tracks
        # devices currently being kicked, so the same device is never handed to
        # two workers at once.
        self._claim_lock = threading.Lock()
        self._cursor = 0
        self._claimed = 0
        self._in_flight = set()
        # Per-device cooldown: device -> earliest time it may be kicked again.
        # Reproduces the original's "one device, one kick per BF_KICK_DELAY"
        # rhythm while still letting different devices run in parallel.
        self._device_next = {}
        self._wait_hint = 0.05
        self._stop_event = threading.Event()

    # ── work distribution ──────────────────────────────────────────────────
    def _next_device(self):
        """Claim the next kickable device.

        Returns (device, None) when there is work, (None, _WAIT) when every
        remaining device is paused — the caller should wait, not finish — and
        (None, None) when the run is done.

        A device is only handed out when NO worker is already kicking it. The
        original BF Kicker walks the list with a single thread, so each device
        is kicked once per round; without this guard a run of one device and 20
        workers kicks that same device 20 times at once (measured: peak
        concurrency 20, 16 kicks/s on one device). That is what makes a device
        look like a bot and get the login server to start refusing.
        """
        with self._claim_lock:
            if not self.job.alive:
                return None, None
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
                    # Another worker is already kicking this device. Skip it and
                    # try the next one, so two workers never hit the same device.
                    continue
                if self._device_next.get(dev, 0.0) > time.time():
                    # This device is still cooling down from its last kick.
                    # Pacing is per DEVICE, which is what the original does with
                    # its single thread: one device is contacted about once per
                    # BF_KICK_DELAY. Different devices still run in parallel.
                    cooling = True
                    continue
                self._claimed += 1
                self._in_flight.add(dev)
                # passes = how many times the whole list has been walked. Counting
                # it at claim time (not only on wrap) keeps the last partial
                # round included: 8 kicks over 4 devices is 2 passes, not 1.
                self.job.set_passes(self._claimed // n)
                return dev, None
            # A full sweep found nothing kickable right now.
            if self._in_flight or cooling:
                # Work exists, it is just in flight or cooling down. Wait for it
                # rather than kicking the same device again straight away. The
                # hint is how long until the earliest device frees up, so the
                # worker wakes exactly then instead of polling on a fixed timer
                # (a 0.4s poll turned a 0.5s cooldown into ~0.8s per kick).
                soonest = min([self._device_next.get(d, 0.0)
                               for d in self.job.devices] or [0.0])
                hint = soonest - time.time() if soonest > time.time() else 0.05
                self._wait_hint = max(0.02, min(hint, 0.25))
                return None, _WAIT
            if self.job.has_paused():
                self.job.note = "semua device di-pause"
                return None, _WAIT
            if self.job.stopped_set():
                self.job.note = "semua device distop"
            return None, None

    def _release_device(self, device):
        """Free a device once its kick finished, so a later round can take it."""
        with self._claim_lock:
            self._in_flight.discard(device)

    def _refund(self):
        """Give back a reserved budget slot for a claim that was never sent."""
        with self._claim_lock:
            if self._claimed > 0:
                self._claimed -= 1

    def _kick(self, device):
        """Returns (ok, msg, profile), or None when the claim was skipped."""
        got = _SLOTS.acquire(self.job.run_id, self.workers_cap)
        if not got:
            # Timed out waiting for a slot (pool busy). Skip; the budget is
            # refunded by the caller so this never eats a kick.
            return None
        try:
            if not self.job.alive or self.job.is_stopped(device):
                return None
            if self.job.is_paused(device):
                # Paused between claim and send. Skipped, and the budget slot is
                # returned by the caller so a pause never silently eats a kick.
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
                    # Idle on purpose (everything paused, or all devices in
                    # flight / cooling down). Wait the computed hint so a resume
                    # is picked up quickly and a cooldown expires on time.
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
            # Start this DEVICE's cooldown. The original BF Kicker walks the list
            # with a single thread and sleeps BF_KICK_DELAY between kicks, so one
            # device is contacted about once per 0.5s. Pacing per device keeps
            # that rhythm for each device while still letting different devices
            # run in parallel - a per-worker sleep did not, because the kick
            # itself (~790 ms) already outlasts the delay, and a global gate
            # would have made extra workers useless.
            if self.kick_delay > 0:
                with self._claim_lock:
                    self._device_next[device] = time.time() + self.kick_delay

    def run(self):  # noqa: D401 - Thread entry point
        # Register before starting workers: the slot pool divides the global cap
        # by the number of live runs, so this run must already be counted.
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
