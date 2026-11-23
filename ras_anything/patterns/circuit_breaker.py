"""Circuit breaker: stop calling a dependency that keeps failing; probe it before trusting it again."""

from __future__ import annotations

import threading
from collections import deque

from .. import runtime as rt
from . import ADOPTED, Layer, Pattern


class CircuitOpenError(rt.RejectedError):
    """Raised without calling the dependency while the breaker is open."""


class CircuitBreaker:
    """Opens after `fail_threshold` consecutive failures, or when at least `failure_rate` of the last `window`
    calls failed (once there were `min_calls`; None switches this rule off). After `cooldown_s` it is half-open:
    up to `half_open_probes` calls at a time get through; that many successes close it, one failure re-opens it.

    Only errors `count` accepts are failures; other exceptions (a 404, a bug) say nothing about the
    dependency. Outcomes of calls admitted before the breaker last changed state are ignored."""

    def __init__(self, fail_threshold: int | None = 5, cooldown_s: float = 30.0, name: str = "dependency",
                 count=rt.dependency_failed, *, failure_rate: float | None = None, window: int = 50,
                 min_calls: int = 20, half_open_probes: int = 1):
        if half_open_probes < 1 or window < 1:
            raise ValueError("half_open_probes and window must be >= 1")
        self.fail_threshold, self.cooldown_s, self.name, self.count = fail_threshold, cooldown_s, name, count
        self.failure_rate, self.window, self.min_calls, self.half_open_probes = failure_rate, window, min_calls, half_open_probes
        self._lock = threading.Lock()
        self._gen = 0
        self._clear()
        rt.track(self)

    def _clear(self) -> None:
        self.failures = 0                    # consecutive
        self.opened_at: float | None = None
        self._recent: deque = deque(maxlen=self.window)
        self._probes = self._probe_successes = 0
        self._gen += 1

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if rt._now() - self.opened_at >= self.cooldown_s else "open"

    def reset(self) -> None:
        with self._lock:
            self._clear()

    def acquire(self):
        """Admit one call: a ticket for release(), or CircuitOpenError."""
        with self._lock:
            st = self.state
            if st == "open" or (st == "half_open" and self._probes >= self.half_open_probes):
                raise CircuitOpenError("circuit open for %s" % self.name,
                                       context={"failures": self.failures, "cooldown_s": self.cooldown_s})
            if st == "half_open":
                self._probes += 1
            return self._gen, st == "half_open"

    def release(self, ticket, error: BaseException | None = None) -> None:
        """Say how the admitted call ended."""
        gen, probe = ticket
        with self._lock:
            if gen != self._gen:
                return
            if probe:
                self._probes -= 1
            if error is None:
                self.failures = 0
                if probe:
                    self._probe_successes += 1
                    if self._probe_successes >= self.half_open_probes:
                        self._clear()
                else:
                    self._recent.append(False)
            elif self.count(error):
                self.failures += 1
                if probe:
                    self._open()
                else:
                    self._recent.append(True)
                    if self._tripped():
                        self._open()

    def _tripped(self) -> bool:
        if self.fail_threshold is not None and self.failures >= self.fail_threshold:
            return True
        n = len(self._recent)
        return self.failure_rate is not None and n >= self.min_calls and sum(self._recent) >= self.failure_rate * n

    def _open(self) -> None:
        failures = self.failures
        self._clear()
        self.failures, self.opened_at = failures, rt._now()

    def call(self, fn, *args, **kwargs):
        ticket = self.acquire()
        try:
            result = fn(*args, **kwargs)
        except BaseException as e:
            self.release(ticket, e)
            raise
        self.release(ticket)
        return result

    __call__ = call

    async def acall(self, fn, *args, **kwargs):
        """call() for an async fn."""
        ticket = self.acquire()
        try:
            result = await fn(*args, **kwargs)
        except BaseException as e:
            self.release(ticket, e)
            raise
        self.release(ticket)
        return result


class CircuitBreakerLayer(Layer):
    def __init__(self, dependency, params):
        super().__init__(dependency, params)
        p = dict(params)
        self.breaker = CircuitBreaker(p.pop("fail_threshold"), p.pop("cooldown_s"), dependency, **p)

    def run(self, call, inner):
        ticket = self.breaker.acquire()
        try:
            result = yield from inner(call)
        except BaseException as e:
            self.breaker.release(ticket, e)
            raise
        self.breaker.release(ticket)
        return result


PATTERN = Pattern(
    name="circuit_breaker", title="Circuit breaker", order=80, verdict=ADOPTED,
    hint="fails fast (a RejectedError) while the dependency is down, probes once per cooldown. A 429 doesn't open it. Nothing to do.",
    params={"fail_threshold": 5, "cooldown_s": 1.0, "failure_rate": 0.5, "window": 50, "min_calls": 20, "half_open_probes": 1},
    layer=CircuitBreakerLayer, exports=("CircuitBreaker", "CircuitOpenError"),
    refs=("Release It! 2e: Circuit Breaker", "resilience4j: CircuitBreaker (sliding window, half-open calls)", "Polly v8: circuit breaker",
          "AWS REL05-BP01: graceful degradation"))
