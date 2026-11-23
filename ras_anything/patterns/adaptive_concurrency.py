"""Adaptive concurrency: an AIMD limit on calls in flight (like TCP): +1 per round of successes, x backoff on failure."""

from __future__ import annotations

import threading

from .. import runtime as rt
from . import OPT_IN, Layer, Pattern


class ConcurrencyLimitError(rt.RejectedError):
    """Raised without calling the dependency: the current concurrency limit is reached."""


class AdaptiveConcurrency:
    """Admits while in_flight < limit. A success while the limit is in use adds 1/limit, an error `count`
    accepts multiplies the limit by `backoff` (at most once per limit's worth of calls), within [min, max]."""

    def __init__(self, initial: float = 20.0, min_limit: float = 1.0, max_limit: float = 200.0, backoff: float = 0.7,
                 name: str = "dependency", count=rt.dependency_failed):
        self.initial, self.min_limit, self.max_limit, self.backoff = float(initial), float(min_limit), float(max_limit), backoff
        self.name, self.count = name, count
        self._lock = threading.Lock()
        self.in_flight = 0
        self.reset()
        rt.track(self)

    def reset(self) -> None:
        with self._lock:
            self.limit = self.initial
            self._ended = self._cut_at = 0  # calls ended; that count when the limit was last cut

    def acquire(self) -> None:
        with self._lock:
            if self.in_flight >= int(self.limit):
                raise ConcurrencyLimitError("concurrency limit for %s" % self.name, context={"limit": int(self.limit)})
            self.in_flight += 1

    def release(self, error: BaseException | None = None) -> None:
        with self._lock:
            used = self.in_flight
            self.in_flight -= 1
            self._ended += 1
            if error is not None and self.count(error):
                if self._ended - self._cut_at >= self.limit or self._cut_at == 0:
                    self.limit = max(self.min_limit, self.limit * self.backoff)
                    self._cut_at = self._ended
            elif error is None and used * 2 >= self.limit:
                self.limit = min(self.max_limit, self.limit + 1.0 / self.limit)

    def call(self, fn, *args, **kwargs):
        self.acquire()
        try:
            result = fn(*args, **kwargs)
        except BaseException as e:
            self.release(e)
            raise
        self.release()
        return result

    async def acall(self, fn, *args, **kwargs):
        """call() for an async fn."""
        self.acquire()
        try:
            result = await fn(*args, **kwargs)
        except BaseException as e:
            self.release(e)
            raise
        self.release()
        return result


class AdaptiveConcurrencyLayer(Layer):
    def __init__(self, dependency, params):
        super().__init__(dependency, params)
        self.limiter = AdaptiveConcurrency(name=dependency, **params)

    def run(self, call, inner):
        self.limiter.acquire()
        try:
            result = yield from inner(call)
        except BaseException as e:
            self.limiter.release(e)
            raise
        self.limiter.release()
        return result


PATTERN = Pattern(
    name="adaptive_concurrency", title="Adaptive concurrency limit (AIMD)", order=100, verdict=OPT_IN, why="",
    params={"initial": 20.0, "min_limit": 1.0, "max_limit": 200.0, "backoff": 0.7}, layer=AdaptiveConcurrencyLayer,
    exports=("AdaptiveConcurrency", "ConcurrencyLimitError"),
    refs=("Netflix concurrency-limits (AIMD)", "Google SRE ch.22: limit in-flight requests", "Release It! 2e: Governor, Create Back Pressure"))
