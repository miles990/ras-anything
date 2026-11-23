"""Policy: the patterns composed around one dependency, in a fixed order, sync or async.

    weather = Policy("weather-api", deadline={"timeout_s": 4.8}, attempt_timeout={"timeout_s": 3.5})

    def fetch(city):
        return weather.call(lambda a: urlopen(url, timeout=a.timeout_s).read())

Adopted patterns are on, the others off; a keyword named after a pattern switches it (True/False) or sets
its parameters (a dict). Order, outermost first: fallback, deadline, bulkhead, idempotency, retry budget,
Retry-After, retry, circuit breaker, adaptive throttle, adaptive concurrency, hedge, attempt timeout.
Create a policy once per dependency (module level): its state is what it has learned about it.
"""

from __future__ import annotations

import contextvars
import queue
import threading
import time

from .. import runtime as rt
from . import Call, all_patterns, is_on


class Attempt:
    """What the user's function gets: one attempt of one logical call."""

    __slots__ = ("n", "timeout_s", "idempotency_key", "headers", "_cancel")

    def __init__(self, n, timeout_s=None, idempotency_key=None, headers=None):
        self.n = n                              # 1, 2, ... over the whole call, hedges included
        self.timeout_s = timeout_s              # give it to the boundary call (None: no limit)
        self.idempotency_key = idempotency_key  # the same in every attempt, or None
        self.headers = dict(headers or {})      # send these
        self._cancel = threading.Event()

    @property
    def cancelled(self) -> bool:
        """True once another attempt won: a hedge that can stop early should."""
        return self._cancel.is_set()


class Policy:
    def __init__(self, dependency: str, **patterns):
        self.dependency = dependency
        self._kwargs = patterns
        self._shards = {}
        self.layers = []
        known = {p.name: p for p in all_patterns()}
        unknown = sorted(set(patterns) - set(known))
        if unknown:
            raise TypeError("unknown pattern %s (known: %s)" % (", ".join(unknown), ", ".join(known)))
        for p in known.values():
            given = patterns.get(p.name)
            on = p.default_on if given is None else bool(given) or isinstance(given, dict)
            params = dict(p.params)
            if isinstance(given, dict):
                bad = sorted(set(given) - set(params))
                if bad:
                    raise TypeError("%s has no parameter %s (it has: %s)" % (p.name, ", ".join(bad), ", ".join(params) or "none"))
                params.update(given)
            self.layers.append((p.name, on, p.layer(dependency, params)))

    def layer(self, name: str):
        """The layer of a pattern (its state: breaker, budget, ...), switched on or not."""
        return next(x for n, _, x in self.layers if n == name)

    def shard(self, key: str) -> "Policy":
        """A policy with the same patterns whose breaker/budget/cache are this key's alone.

        Isolation is opt-in: one failing key cannot open the breaker the others use.
        """
        if key in self._shards:
            return self._shards[key]
        child = Policy("%s#%s" % (self.dependency, key), **self._kwargs)
        self._shards[key] = child
        return child

    def _steps(self, method, idempotency_key, cache_key):
        call = Call(self.dependency, method=method, idempotency_key=idempotency_key, cache_key=cache_key)
        on = [x for name, configured, x in self.layers if is_on(self.dependency, name, configured)]

        def attempt(c):
            t = c.timeout_s
            if t is None and c.deadline is not None:  # no attempt timeout: what is left of the whole
                t = max(0.001, c.deadline - rt._now())
            return (yield ("call", Attempt(next(c.numbers), t, c.carry_key, c.headers), _ends(c)))

        def chain(i):
            return attempt if i == len(on) else lambda c: on[i].run(c, chain(i + 1))

        return chain(0)(call)

    def call(self, fn, *, method: str = "GET", idempotency_key: str | None = None, cache_key=None):
        """fn(attempt) under this policy. `method` other than GET/HEAD/OPTIONS/RUN is a write: it is only
        repeated when idempotency_key is given (send attempt.headers with every attempt)."""
        return drive(self._steps(method, idempotency_key, cache_key), fn)

    async def acall(self, fn, *, method: str = "GET", idempotency_key: str | None = None, cache_key=None):
        """call() for an async fn(attempt). Attempt timeouts are enforced (the await is cancelled)."""
        return await adrive(self._steps(method, idempotency_key, cache_key), fn)


def _ends(c):
    """The deadline the attempt's own nested calls must respect (None when deadlines are off)."""
    if c.deadline is None:
        return None
    return c.deadline if c.timeout_s is None else min(c.deadline, rt._now() + c.timeout_s)


# --------------------------------------------------------------------------- drivers


def drive(steps, fn):
    """Runs a layer generator, blocking."""
    op, value, error = None, None, None
    while True:
        try:
            op = steps.throw(error) if error is not None else steps.send(value)
        except StopIteration as e:
            return e.value
        value, error = None, None
        try:
            if op[0] == "call":
                token = rt.set_deadline(op[2])
                try:
                    value = fn(op[1])
                finally:
                    rt.clear_deadline(token)
            elif op[0] == "sleep":
                time.sleep(op[1])
            else:
                value = _race(op, fn)
        except BaseException as e:
            error = e


async def adrive(steps, fn):
    """Runs a layer generator on the event loop."""
    import asyncio

    op, value, error = None, None, None
    while True:
        try:
            op = steps.throw(error) if error is not None else steps.send(value)
        except StopIteration as e:
            return e.value
        value, error = None, None
        try:
            if op[0] == "call":
                token = rt.set_deadline(op[2])
                try:
                    t = op[1].timeout_s
                    value = await (fn(op[1]) if t is None else asyncio.wait_for(fn(op[1]), t))
                finally:
                    rt.clear_deadline(token)
            elif op[0] == "sleep":
                await asyncio.sleep(op[1])
            else:
                value = await _arace(op, fn)
        except BaseException as e:
            error = e


def _watching(steps, mine):
    """`steps`, noting the attempts it makes (so the losers can be told)."""
    value, error = None, None
    while True:
        try:
            op = steps.throw(error) if error is not None else steps.send(value)
        except StopIteration as e:
            return e.value
        value, error = None, None
        if op[0] == "call":
            mine.append(op[1])
        try:
            value = yield op
        except GeneratorExit:
            steps.close()
            raise
        except BaseException as e:
            error = e


def _race(op, fn):
    """Branch 0 now; each further one after delay_s without an answer, while gate() allows it. First success
    wins; the losers are told (attempt.cancelled) and their results dropped."""
    _, make, delay_s, gate = op
    if rt.VIRTUAL_CLOCK:
        return _race_virtual(op, fn)
    done: queue.SimpleQueue = queue.SimpleQueue()
    branches: list[list] = []

    def start():
        i, mine = len(branches), []
        branches.append(mine)
        ctx = contextvars.copy_context()

        def run():
            try:
                done.put((None, ctx.run(drive, _watching(make(i), mine), fn)))
            except BaseException as e:  # handed to the caller below
                done.put((e, None))

        threading.Thread(target=run, name="ras-hedge", daemon=True).start()

    start()
    pending, first, may = 1, None, True
    while pending:
        try:
            err, value = done.get(timeout=delay_s if may else None)
        except queue.Empty:
            may = gate(len(branches))
            if may:
                start()
                pending += 1
            continue
        pending -= 1
        if err is None:
            for mine in branches:
                for a in mine:
                    a._cancel.set()
            return value
        first = first or err
        if len(branches) == 1:
            break  # it failed before a hedge was due: that is the retry layer's business
        may = False  # the others decide
    raise first


def _race_virtual(op, fn):
    """The same race on the virtual clock, where nothing runs concurrently: branches run one after the other,
    each from its own start time, and the clock ends where the race would have ended."""
    _, make, delay_s, gate = op
    clock = rt.VIRTUAL_CLOCK
    t0, ends = clock.t, []
    for i in range(64):
        start = t0 + i * delay_s
        if i and (any(end <= start for end, _, _ in ends) or not gate(i)):  # something ended first, or no hedge allowed
            break
        clock.t = start
        try:
            value, err = drive(make(i), fn), None
        except Exception as e:
            value, err = None, e
        ends.append((clock.t, err, value))
    good = [x for x in ends if x[1] is None]
    end, err, value = min(good, key=lambda x: x[0]) if good else (max(e for e, _, _ in ends), ends[0][1], None)
    clock.t = end
    if err is not None:
        raise err
    return value


async def _arace(op, fn):
    import asyncio

    _, make, delay_s, gate = op
    tasks, branches = [], []

    def start():
        mine: list = []
        branches.append(mine)
        tasks.append(asyncio.ensure_future(adrive(_watching(make(len(tasks)), mine), fn)))

    start()
    may = True
    try:
        while True:
            pending = [t for t in tasks if not t.done()]
            await asyncio.wait(pending, timeout=delay_s if may else None, return_when=asyncio.FIRST_COMPLETED)
            good = next((t for t in tasks if t.done() and not t.cancelled() and t.exception() is None), None)
            if good is not None:
                return good.result()
            if all(t.done() for t in tasks):
                raise tasks[0].exception() or asyncio.CancelledError()
            if len(tasks) > sum(not t.done() for t in tasks):
                may = False  # one failed: no more hedges, the others decide
            elif may:
                may = gate(len(tasks))
                if may:
                    start()
    finally:
        for mine in branches:
            for a in mine:
                a._cancel.set()
        losers = [t for t in tasks if not t.done()]
        for t in losers:
            t.cancel()
        if losers:
            await asyncio.gather(*losers, return_exceptions=True)
