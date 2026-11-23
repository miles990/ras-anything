"""Hedged requests: when an idempotent read is slow, send a second one and take whichever answers first."""

from __future__ import annotations

import threading
from collections import deque

from .. import runtime as rt
from . import OPT_IN, Layer, Pattern, SAFE_METHODS


class LatencyTracker:
    """The q-quantile of the last `keep` successful attempts, once there were `least` of them."""

    def __init__(self, quantile: float = 0.95, keep: int = 100, least: int = 20):
        self.quantile, self.least = quantile, least
        self._lock = threading.Lock()
        self._seen: deque = deque(maxlen=keep)
        rt.track(self)

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()

    def record(self, seconds: float) -> None:
        with self._lock:
            self._seen.append(seconds)

    def value(self) -> float | None:
        with self._lock:
            xs = sorted(self._seen)
        return xs[min(len(xs) - 1, int(self.quantile * len(xs)))] if len(xs) >= self.least else None


def _require_read(method: str) -> None:
    if method.upper() not in SAFE_METHODS:
        raise ValueError("only idempotent reads may be hedged, not %s" % method)


def _plan(n_max, gate):
    return lambda started: started <= n_max and (gate is None or gate())


def hedge(fn, *, delay_s: float, max_hedges: int = 1, method: str = "GET", retry_budget=None):
    """fn(attempt); if it hasn't answered after delay_s, again (up to max_hedges more), and the first answer
    wins. Reads only. The losers can't be interrupted in blocking code: they should look at attempt.cancelled
    and need their own timeout. With a retry_budget each hedge also needs a token."""
    from .policy import Attempt, drive

    _require_read(method)
    return drive(_steps(Attempt, delay_s, max_hedges, retry_budget), fn)


async def ahedge(fn, *, delay_s: float, max_hedges: int = 1, method: str = "GET", retry_budget=None):
    """hedge() for an async fn: the losers are cancelled."""
    from .policy import Attempt, adrive

    _require_read(method)
    return await adrive(_steps(Attempt, delay_s, max_hedges, retry_budget), fn)


def _steps(Attempt, delay_s, max_hedges, retry_budget):
    if retry_budget is not None:
        retry_budget.record_request()

    def branch(i):
        return (yield ("call", Attempt(i + 1), None))

    return (yield ("race", branch, delay_s, _plan(max_hedges, retry_budget.try_spend if retry_budget else None)))


class HedgeLayer(Layer):
    """After delay_s, or after the quantile of what this dependency's answers usually take, once known."""

    def __init__(self, dependency, params):
        super().__init__(dependency, params)
        self.latency = LatencyTracker(params["quantile"])

    def run(self, call, inner):
        if not call.safe or call.carry_key is not None:
            return (yield from inner(call))

        def branch(i):
            t0 = rt._now()
            result = yield from inner(call)
            self.latency.record(rt._now() - t0)
            return result

        known = self.latency.value()
        delay = self.params["delay_s"] if known is None else max(self.params["min_delay_s"], known)
        gates = list(call.retry_gates)
        return (yield ("race", branch, delay, _plan(self.params["max_hedges"], lambda: all(g() for g in gates))))


PATTERN = Pattern(
    name="hedge", title="Hedged requests (reads)", order=110, verdict=OPT_IN, why="",
    params={"delay_s": 1.0, "min_delay_s": 0.01, "quantile": 0.95, "max_hedges": 1}, layer=HedgeLayer, exports=("hedge", "ahedge"),
    refs=("Dean & Barroso, The Tail at Scale: hedged requests", "Polly v8: hedging", "Failsafe: Hedge", "gRPC hedging (retry throttling applies)"))
