"""Retry: exponential backoff with full jitter, a limited number of attempts, transient errors only."""

from __future__ import annotations

import time

from .. import runtime as rt
from . import ADOPTED, Layer, Pattern


def retry(fn, *, attempts: int = 3, base: float = 0.1, cap: float = 2.0, budget_s: float | None = None,
          retry_on=rt.is_transient, sleep=None, on_retry=None, retry_budget=None):
    """Call fn() up to `attempts` times, retrying only errors `retry_on` accepts.

    Backoff is "full jitter" (AWS): sleep = random(0, min(cap, base * 2**n)), at least the
    server's Retry-After when it sent one. `budget_s` bounds the total time, and so does a
    deadline() around it: a retry whose wait would cross either is not attempted, and
    attempt_timeout() inside fn is cut to what is left. With a
    `retry_budget` (RetryBudget, shared by all calls of a dependency) a retry also needs a token,
    unless the server itself asked for it with Retry-After.
    The last error is re-raised unchanged (same type), with a note listing the attempts.
    `retry_on` is a predicate or a tuple of exception types. `sleep` defaults to time.sleep looked
    up at call time.
    """
    step = _backoff(attempts, base, cap, budget_s, retry_on, on_retry, retry_budget)
    end = None if budget_s is None else rt._now() + budget_s
    while True:
        token = rt.set_deadline(end)
        try:
            return fn()
        except Exception as e:
            rt.clear_deadline(token)
            token = None
            delay = step(e)
            if delay is None:
                raise
        finally:
            if token is not None:
                rt.clear_deadline(token)
        (sleep or time.sleep)(delay)


async def aretry(fn, *, attempts: int = 3, base: float = 0.1, cap: float = 2.0, budget_s: float | None = None,
                 retry_on=rt.is_transient, on_retry=None, retry_budget=None):
    """retry() for an async fn: awaits fn() and waits with asyncio.sleep."""
    import asyncio

    step = _backoff(attempts, base, cap, budget_s, retry_on, on_retry, retry_budget)
    end = None if budget_s is None else rt._now() + budget_s
    while True:
        token = rt.set_deadline(end)
        try:
            return await fn()
        except Exception as e:
            rt.clear_deadline(token)
            token = None
            delay = step(e)
            if delay is None:
                raise
        finally:
            if token is not None:
                rt.clear_deadline(token)
        await asyncio.sleep(delay)


def jitter(n: int, base: float, cap: float, full: bool = True) -> float:
    """Seconds to wait after the n-th failure (n = 1, 2, ...)."""
    top = min(cap, base * (2 ** (n - 1)))
    return rt._rng.uniform(0.0, top) if full else top


def _backoff(attempts, base, cap, budget_s, retry_on, on_retry, retry_budget):
    """step(error) -> seconds to wait before the next attempt, or None to give up (the error gets a note)."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    if isinstance(retry_on, (type, tuple)):
        types = retry_on
        retry_on = lambda e: isinstance(e, types)  # noqa: E731
    start, n = rt._now(), 0
    if retry_budget is not None:
        retry_budget.record_request()

    def step(e):
        nonlocal n
        n += 1
        if n >= attempts or not retry_on(e):
            rt.note(e, "ras retry: gave up after %d attempt(s)" % n)
            return None
        delay = jitter(n, base, cap)
        ra = rt.retry_after_s(e)
        if ra is not None:
            delay = max(delay, ra)
        if budget_s is not None and (rt._now() - start) + delay >= budget_s:
            rt.note(e, "ras retry: stopped after %d attempt(s), budget %.3gs exhausted" % (n, budget_s))
            return None
        left = rt.remaining_s()
        if left is not None and delay >= left:
            rt.note(e, "ras retry: stopped after %d attempt(s), the deadline comes first" % n)
            return None
        if retry_budget is not None and ra is None and not retry_budget.try_spend():
            rt.note(e, "ras retry: stopped after %d attempt(s), retry budget used up" % n)
            return None
        if on_retry is not None:
            on_retry(n, e, delay)
        return delay

    return step


class RetryLayer(Layer):
    def run(self, call, inner):
        p = self.params
        retry_on = p["retry_on"] or rt.is_transient
        n = 0
        while True:
            try:
                return (yield from inner(call))
            except Exception as e:
                n += 1
                if n >= p["attempts"] or not call.repeatable or not (retry_on(e) or any(r(e) for r in call.retry_rules)):
                    raise
                delay = jitter(n, p["base_s"], p["cap_s"], p["full_jitter"])
                call.invited = False
                for rule in call.delay_rules:
                    delay = rule(e, delay)
                if call.deadline is not None and rt._now() + delay >= call.deadline:
                    rt.note(e, "ras retry: stopped after %d attempt(s), the deadline comes first" % n)
                    raise
                if not call.invited and not all(gate() for gate in call.retry_gates):
                    rt.note(e, "ras retry: stopped after %d attempt(s), retry budget used up" % n)
                    raise
            yield ("sleep", delay)


PATTERN = Pattern(
    name="retry", title="Retry with backoff and full jitter", order=70, verdict=ADOPTED,
    hint="transient errors only, exponential backoff, full jitter.",
    params={"attempts": 3, "base_s": 0.1, "cap_s": 2.0, "full_jitter": True, "retry_on": None},
    layer=RetryLayer, exports=("retry", "aretry", "is_transient"),
    refs=("AWS Builders' Library: Timeouts, retries, and backoff with jitter", "AWS Architecture Blog: Exponential Backoff And Jitter (full jitter)",
          "Google SRE ch.21: at most three attempts per request",
          "AWS REL05-BP03: Control and limit retry calls", "resilience4j / Polly v8 / Failsafe: Retry"))
