"""Retry budget: a token bucket per dependency. Retries may only be a small share of the requests."""

from __future__ import annotations

import threading

from .. import runtime as rt
from . import ADOPTED, Layer, Pattern


class RetryBudget:
    """Each request adds `ratio` tokens, each second `min_per_s`, up to `burst`; each retry takes one.

    So over any stretch of time: retries <= burst + ratio x requests + min_per_s x seconds. When the
    dependency is fine nobody notices; when it is down, retries stop multiplying the load."""

    def __init__(self, ratio: float = 0.1, min_per_s: float = 1.0, burst: float = 10.0):
        self.ratio, self.min_per_s, self.burst = ratio, min_per_s, burst
        self._lock = threading.Lock()
        self.reset()
        rt.track(self)

    def reset(self) -> None:
        with self._lock:
            self._tokens, self._at = float(self.burst), rt._now()

    def _refill(self) -> None:
        now = rt._now()
        self._tokens = min(self.burst, self._tokens + self.min_per_s * max(0.0, now - self._at))
        self._at = now

    @property
    def tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens

    def record_request(self) -> None:
        """One logical call begins (not one attempt)."""
        with self._lock:
            self._refill()
            self._tokens = min(self.burst, self._tokens + self.ratio)

    def try_spend(self) -> bool:
        """May one retry be made now? Takes its token if so."""
        with self._lock:
            self._refill()
            if self._tokens < 1.0 - 1e-9:
                return False
            self._tokens -= 1.0
            return True


class RetryBudgetLayer(Layer):
    def __init__(self, dependency, params):
        super().__init__(dependency, params)
        self.budget = RetryBudget(**params)

    def run(self, call, inner):
        self.budget.record_request()
        call.retry_gates.append(self.budget.try_spend)  # asked by retries and by hedges
        return (yield from inner(call))


PATTERN = Pattern(
    name="retry_budget", title="Retry budget (token bucket)", order=50, verdict=ADOPTED, why="",
    params={"ratio": 0.1, "min_per_s": 1.0, "burst": 10.0}, layer=RetryBudgetLayer, exports=("RetryBudget",),
    refs=("Google SRE ch.21: per-client retry budget (10%)", "AWS Builders' Library: token bucket for retries",
          "Finagle RetryBudget", "AWS REL05-BP03: Control and limit retry calls"))
