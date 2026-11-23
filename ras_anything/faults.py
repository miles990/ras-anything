"""The fault model: which fault hits which boundary attempt (deterministic, seeded) and what it does.

A *request key* identifies one logical dependency request (method + url + body hash, or a command
line). Faults are decided per (seed, key, n-th attempt of that key), so the original and the
hardened code see the *same* fault on the same attempt even though they retry differently.
"""

from __future__ import annotations

import asyncio
import hashlib
import selectors
import time

KINDS = ("timeout", "refused", "http5xx", "http429", "slow", "truncated")
MATRIX = KINDS + ("outage",)

DESCRIBE = {
    "timeout": "no answer: caller's timeout fires; with no timeout it hangs (POST/PUT still reach the server)",
    "refused": "connection refused",
    "http5xx": "HTTP 503 (subprocess: exit 75 EX_TEMPFAIL)",
    "http429": "HTTP 429 with Retry-After: 1",
    "slow": "+3 s latency, then the correct answer",
    "truncated": "body (or stdout) cut in half",
    "outage": "dependency down (refused) for 3 consecutive attempts, then recovers",
    "mixed": "seeded mix: each attempt faults with p=0.3, kind drawn uniformly",
}

SLOW_S = 3.0          # latency the "slow" fault adds (virtual seconds)
HANG_CAP_S = 60.0     # how long a call with no timeout "hangs" before we call it a hang (virtual)
OUTAGE_K = 3          # consecutive failing attempts in an outage
RETRY_AFTER = "1"
CONSTANTS = {"slow_s": SLOW_S, "hang_cap_s": HANG_CAP_S, "outage_k": OUTAGE_K, "retry_after": RETRY_AFTER}  # for agents


class ChaosHang(BaseException):
    """Raised when a call with no timeout would have blocked forever. A BaseException on purpose:
    `except Exception` must not be able to hide a hang."""


def _u(*parts) -> float:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2.0 ** 64


class Plan:
    """Decides the fault (or None) for the n-th attempt of a request key."""

    name = "none"

    def fault(self, key: str, n: int, nonidempotent: bool = False) -> str | None:
        return None


class FirstAttempt(Plan):
    """The first attempt of every distinct request (or just `target_key`) gets `kind`;
    outage = first OUTAGE_K attempts."""

    def __init__(self, kind: str, target_key: str | None = None):
        self.kind = self.name = kind
        self.target_key = target_key

    def fault(self, key, n, nonidempotent=False):
        if self.target_key is not None and key != self.target_key:
            return None
        if self.kind == "outage":
            return "refused" if n < OUTAGE_K else None
        return self.kind if n == 0 else None


class Seeded(Plan):
    """Each attempt faults with probability `rate` (kind uniform over KINDS); with probability
    `outage_rate` a request key starts with an OUTAGE_K-attempt outage."""

    def __init__(self, seed: int, rate: float = 0.3, outage_rate: float = 0.0, salt: str = ""):
        self.seed, self.rate, self.outage_rate, self.salt = seed, rate, outage_rate, salt
        self.name = "mixed"

    def fault(self, key, n, nonidempotent=False):
        if self.outage_rate and _u(self.seed, self.salt, key, "outage") < self.outage_rate and n < OUTAGE_K:
            return "refused"
        if _u(self.seed, self.salt, key, n, "p") < self.rate:
            return KINDS[int(_u(self.seed, self.salt, key, n, "k") * len(KINDS)) % len(KINDS)]
        return None


def schedule(plan: Plan, keys: list[str], attempts: int = 4) -> list[list[str | None]]:
    """The faults a plan would inject for the first `attempts` attempts of each key (for display
    and for the determinism test)."""
    return [[plan.fault(k, n) for n in range(attempts)] for k in keys]


# --------------------------------------------------------------------------- clocks


class LiveClock:
    """Real time, with injected waits scaled down by `scale`; nothing blocks longer than `hang_s`
    (a 300 s client timeout shouldn't cost 15 s of chaos run)."""

    virtual = False

    def __init__(self, scale: float = 0.05, hang_s: float = 2.0):
        self.scale, self.hang_s = scale, hang_s

    def now(self) -> float:
        return time.monotonic()

    def wait(self, s: float) -> None:
        if s > 0:
            time.sleep(min(s * self.scale, self.hang_s))

    async def await_(self, s: float) -> None:
        if s > 0:
            await asyncio.sleep(min(s * self.scale, self.hang_s))

    async def await_hang(self) -> None:
        await asyncio.sleep(self.hang_s)

    def hang(self) -> None:
        time.sleep(self.hang_s)
        raise ChaosHang("boundary call with no timeout hung (chaos)")


class VirtualClock:
    """Simulated time. `install()` patches time.sleep/monotonic/time/perf_counter, and lets event
    loops that wait for a timer move the clock instead of waiting."""

    virtual = True
    LIMIT_S = HANG_CAP_S * 20  # a call that spends this much virtual time is a hang, whatever it does

    def __init__(self):
        self.t = 1_000_000.0
        self.start = self.t
        self._saved = None

    def now(self) -> float:
        return self.t

    def elapsed(self) -> float:
        return self.t - self.start

    def reset(self) -> None:
        self.start = self.t

    def wait(self, s: float) -> None:
        if s and s > 0:
            self.t += float(s)
        if self.t - self.start > self.LIMIT_S:
            raise ChaosHang("call kept running for %.0f virtual seconds" % (self.t - self.start))

    async def await_(self, s: float) -> None:
        if s and s > 0:
            await asyncio.sleep(s)  # costs no real time: see install

    async def await_hang(self) -> None:
        await asyncio.sleep(HANG_CAP_S)

    def hang(self) -> None:
        self.wait(HANG_CAP_S)
        raise ChaosHang("boundary call with no timeout hung (virtual %.0fs)" % HANG_CAP_S)

    def install(self) -> None:
        import time as _t

        self._saved = (_t.sleep, _t.monotonic, _t.time, _t.perf_counter, _t.monotonic_ns, _t.time_ns,
                       _t.perf_counter_ns)
        base_wall = _t.time()

        def sleep(s):
            if s < 0:
                raise ValueError("sleep length must be non-negative")
            self.wait(s)

        cls = selectors.DefaultSelector
        real_select = cls.select
        self._select = (cls, real_select)

        def select(sel, timeout=None):
            if timeout is None or timeout <= 0:
                return real_select(sel, timeout)
            ready = real_select(sel, 0.002)  # a moment for other threads (executors) to finish
            if ready:
                return ready
            self.wait(timeout)
            return real_select(sel, 0)

        cls.select = select
        _t.sleep = sleep
        _t.monotonic = self.now
        _t.perf_counter = self.now
        _t.time = lambda: base_wall + (self.t - 1_000_000.0)
        _t.monotonic_ns = lambda: int(self.t * 1e9)
        _t.perf_counter_ns = lambda: int(self.t * 1e9)
        _t.time_ns = lambda: int((base_wall + self.t - 1_000_000.0) * 1e9)

    def uninstall(self) -> None:
        if self._saved:
            import time as _t

            (_t.sleep, _t.monotonic, _t.time, _t.perf_counter, _t.monotonic_ns, _t.time_ns,
             _t.perf_counter_ns) = self._saved
            self._saved = None
            cls, real_select = self._select
            cls.select = real_select
