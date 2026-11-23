"""Adaptive throttling (Google SRE): a client that sees rejections starts rejecting its own requests."""

from __future__ import annotations

import threading
from collections import deque

from .. import runtime as rt
from . import OPT_IN, Layer, Pattern


class ThrottledError(rt.RejectedError):
    """Raised without calling the dependency: shed locally because it is rejecting too much."""


class AdaptiveThrottle:
    """Rejects a request with probability max(0, (requests - k * accepts) / (requests + 1)), both counted over
    the last window_s seconds. `requests` counts every attempt asked for, also the ones rejected here;
    `accepts` the ones the dependency answered (`count` says which errors mean it didn't)."""

    def __init__(self, k: float = 2.0, window_s: float = 120.0, name: str = "dependency", count=rt.dependency_failed):
        self.k, self.window_s, self.name, self.count = k, window_s, name, count
        self._lock = threading.Lock()
        self.reset()
        rt.track(self)

    def reset(self) -> None:
        with self._lock:
            self._buckets: deque = deque()  # [bucket number, requests, accepts]
            self.requests = self.accepts = 0

    def _bucket(self):
        now = int(rt._now() / (self.window_s / 24.0))
        while self._buckets and self._buckets[0][0] <= now - 24:
            _, r, a = self._buckets.popleft()
            self.requests -= r
            self.accepts -= a
        if not self._buckets or self._buckets[-1][0] != now:
            self._buckets.append([now, 0, 0])
        return self._buckets[-1]

    def reject_probability(self) -> float:
        with self._lock:
            self._bucket()
            return max(0.0, (self.requests - self.k * self.accepts) / (self.requests + 1.0))

    def acquire(self) -> None:
        """Ask for one attempt; ThrottledError when it is shed."""
        p = self.reject_probability()
        with self._lock:
            self._bucket()[1] += 1
            self.requests += 1
        if p > 0.0 and rt._rng.random() < p:
            raise ThrottledError("shedding load to %s" % self.name, context={"reject_probability": round(p, 3)})

    def release(self, error: BaseException | None = None) -> None:
        """Say how the attempt ended."""
        if error is None or not self.count(error):
            with self._lock:
                self._bucket()[2] += 1
                self.accepts += 1

    def call(self, fn, *args, **kwargs):
        self.acquire()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            self.release(e)
            raise
        self.release()
        return result

    async def acall(self, fn, *args, **kwargs):
        """call() for an async fn."""
        self.acquire()
        try:
            result = await fn(*args, **kwargs)
        except Exception as e:
            self.release(e)
            raise
        self.release()
        return result


class AdaptiveThrottleLayer(Layer):
    def __init__(self, dependency, params):
        super().__init__(dependency, params)
        self.throttle = AdaptiveThrottle(params["k"], params["window_s"], dependency)

    def run(self, call, inner):
        self.throttle.acquire()
        try:
            result = yield from inner(call)
        except Exception as e:
            self.throttle.release(e)
            raise
        self.throttle.release()
        return result


PATTERN = Pattern(
    name="adaptive_throttle", title="Adaptive throttling / load shedding", order=90, verdict=OPT_IN, why="",
    params={"k": 2.0, "window_s": 120.0}, layer=AdaptiveThrottleLayer, exports=("AdaptiveThrottle", "ThrottledError"),
    refs=("Google SRE ch.21: client-side throttling", "AWS Builders' Library: Using load shedding to avoid overload",
          "Release It! 2e: Shed Load, Create Back Pressure"))
