"""The pattern plugins (ras_anything/patterns/), Python side, on the virtual clock: the registry contract every
module must keep, then what each pattern promises. tests/test_patterns_parity.py runs the same stories in Node."""

import ast
import asyncio
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

from ras_anything import faults, patterns, runtime
from ras_anything.patterns import Call, Layer
from ras_anything.runtime import (AdaptiveConcurrency, AdaptiveThrottle, Bulkhead, BulkheadFullError, CircuitBreaker, CircuitOpenError,
                                  ConcurrencyLimitError, DeadlineExceeded, Policy, RejectedError, RetryBudget, StaleCache,
                                  ThrottledError, ahedge, attempt_timeout, deadline, hedge, idempotency_key, remaining_s, retry)

PKG = Path(patterns.__file__).parent
OFF = {n: False for n in patterns.NAMES}


def only(**on):
    """A policy's arguments with every pattern off but these."""
    return {**OFF, **on}


def http_error(code, retry_after=None):
    h = Message()
    if retry_after is not None:
        h["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("http://x", code, "err", h, io.BytesIO(b""))


class VirtualCase(unittest.TestCase):
    """Runs on virtual time; `self.clock.t` is now."""

    def setUp(self):
        self.clock = faults.VirtualClock()
        self.clock.LIMIT_S = 1e9
        self.clock.install()
        self.addCleanup(self.clock.uninstall)
        runtime.VIRTUAL_CLOCK = self.clock
        self.addCleanup(setattr, runtime, "VIRTUAL_CLOCK", None)
        runtime.reset_state()
        runtime.seed(1)
        err = io.StringIO()
        self.err = err
        ctx = contextlib.redirect_stderr(err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def logged(self):
        return [r for r in map(json.loads, self.err.getvalue().splitlines()) if "event" in r]  # (not the runtime's own diagnostics)


class Dep:
    """A dependency following a script: an exception is raised, a number is a slow answer (seconds), else the answer."""

    def __init__(self, *script, forever=None):
        self.script, self.forever, self.attempts, self.at = list(script), forever, [], []

    def _next(self, a):
        self.attempts.append(a)
        self.at.append(time.monotonic())
        return self.script.pop(0) if self.script else self.forever

    def __call__(self, a=None):
        x = self._next(a)
        if isinstance(x, BaseException):
            raise x
        if isinstance(x, (int, float)):
            t = getattr(a, "timeout_s", None)
            if t is not None and x > t:
                time.sleep(t)
                raise TimeoutError("timed out")
            time.sleep(x)
        return "ok"

    async def acall(self, a=None):
        x = self._next(a)
        if isinstance(x, BaseException):
            raise x
        if isinstance(x, (int, float)):
            await asyncio.sleep(x)
        return "ok"


DOWN = ConnectionRefusedError("down")


# ------------------------------------------------------------------------------------------ the contract


class ContractTest(unittest.TestCase):
    def test_registry_is_complete_and_ordered(self):
        ps = patterns.all_patterns()
        self.assertEqual([p.name for p in ps], list(patterns.NAMES))
        self.assertEqual(len({p.order for p in ps}), len(ps))
        files = {f.stem for f in PKG.glob("*.py")} - {"__init__", "policy"}
        self.assertEqual(files - {"problem", "recorder", "ai"}, set(patterns.NAMES))  # (support modules: errors for agents, flight recorder)
        self.assertRaises(KeyError, patterns.get, "nope")

    def test_every_module_keeps_the_contract(self):
        for p in patterns.all_patterns():
            with self.subTest(p.name):
                self.assertTrue(issubclass(p.layer, Layer))
                self.assertIn(p.verdict, (patterns.ADOPTED, patterns.OPT_IN, patterns.REJECTED))
                self.assertTrue(p.title and p.refs)
                for name in p.exports:
                    self.assertTrue(callable(getattr(runtime, name)), name)
                    self.assertIn(name, runtime.__all__)
                json.dumps(p.params)  # a flag file can hold them

    def test_modules_are_independent(self):
        """A pattern imports the core and the contract: no other pattern (hedge may use the drivers), no adapter, no pipeline."""
        for f in PKG.glob("*.py"):
            for n in ast.walk(ast.parse(f.read_text())):
                if isinstance(n, ast.ImportFrom) and n.level == 1 and f.stem not in ("__init__", "policy"):
                    mods = {n.module} if n.module else {a.name for a in n.names} & set(patterns.NAMES) | ({"policy"} & {a.name for a in n.names})
                    self.assertFalse(mods & set(patterns.NAMES), f.name)
                    self.assertFalse("policy" in mods and f.stem != "hedge", f.name)
                elif isinstance(n, ast.ImportFrom) and n.level == 2:
                    self.assertEqual([a.name for a in n.names], ["runtime"], f.name)
                elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                    self.assertFalse(n.module.startswith("ras_anything"), f.name)

    def test_a_layer_alone_passes_results_and_errors_through(self):
        """Any single pattern: a healthy call is made once and returned as is; a bug is raised as is."""
        for p in patterns.all_patterns():
            with self.subTest(p.name):
                pol = Policy("d", **only(**{p.name: True}))
                calls = []
                self.assertEqual(pol.call(lambda a: calls.append(a.n) or {"v": 1}), {"v": 1})
                self.assertEqual(calls, [1])
                bug = KeyError("bug")

                def boom(a):
                    raise bug
                with self.assertRaises(KeyError) as cm:
                    pol.call(boom)
                self.assertIs(cm.exception, bug)

                async def ok(a):
                    return a.n
                self.assertEqual(asyncio.run(pol.acall(ok)), 1)

    def test_any_pattern_can_be_missing(self):
        for p in patterns.all_patterns():
            with self.subTest(p.name):
                pol = Policy("d", **{n: n != p.name for n in patterns.NAMES})
                self.assertEqual(pol.call(lambda a: "x", cache_key="k"), "x")
                with self.assertRaises((ConnectionRefusedError, RejectedError)):
                    Policy("d", **{**{n: n != p.name for n in patterns.NAMES}, "retry": {"base_s": 0}}).call(Dep(forever=DOWN))

    def test_defaults_are_the_adopted_patterns(self):
        on = {name for name, configured, _ in Policy("d").layers if configured}
        self.assertEqual(on, {p.name for p in patterns.all_patterns() if p.verdict == patterns.ADOPTED})

    def test_unknown_patterns_and_parameters_are_errors(self):
        self.assertRaisesRegex(TypeError, "unknown pattern retries", Policy, "d", retries=True)
        self.assertRaisesRegex(TypeError, "no parameter atempts", Policy, "d", retry={"atempts": 3})

    def test_one_switch_decides_for_every_call(self):
        """The hook flags will use: registry.is_on is asked per call."""
        from unittest import mock
        pol = Policy("d", **only(retry={"base_s": 0}))
        with mock.patch("ras_anything.patterns.policy.is_on", lambda dep, name, configured: name != "retry" and configured):
            d = Dep(DOWN)
            self.assertRaises(ConnectionRefusedError, pol.call, d)
        d = Dep(DOWN)
        self.assertEqual((pol.call(d), len(d.attempts)), ("ok", 2))

    def test_core_works_without_the_plugins(self):
        """Removing the folder breaks nothing that doesn't use it."""
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d) / "ras_anything"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            (pkg / "runtime.py").write_text(Path(runtime.__file__).read_text())
            p = subprocess.run([sys.executable, "-c", "from ras_anything.runtime import RasError, call_with_timeout, deadline, log_event, is_transient\n"
                                "print(call_with_timeout(lambda: 5, 1))"], cwd=d, capture_output=True, text=True, timeout=30)
            self.assertEqual((p.stdout, p.stderr), ("5\n", ""))


# ------------------------------------------------------------------------------------------ pattern by pattern


class AttemptTimeoutTest(VirtualCase):
    def test_bounds_an_attempt(self):
        pol = Policy("d", **only(attempt_timeout={"timeout_s": 2.0}))
        d = Dep(10)
        self.assertRaises(TimeoutError, pol.call, d)
        self.assertEqual(d.attempts[0].timeout_s, 2.0)
        self.assertEqual(self.clock.elapsed(), 2.0)

    def test_async_is_enforced_even_if_the_call_ignores_it(self):
        pol = Policy("d", **only(attempt_timeout={"timeout_s": 2.0}))
        with self.assertRaises((TimeoutError, asyncio.TimeoutError)):
            asyncio.run(pol.acall(Dep(10).acall))
        self.assertAlmostEqual(self.clock.elapsed(), 2.0)

    def test_cut_to_what_is_left_of_the_deadline(self):
        pol = Policy("d", **only(deadline={"timeout_s": 3.0}, retry={"attempts": 5, "base_s": 0}, attempt_timeout={"timeout_s": 2.0}))
        d = Dep(forever=10)
        self.assertRaises(TimeoutError, pol.call, d)
        self.assertEqual([a.timeout_s for a in d.attempts], [2.0, 1.0])
        self.assertEqual(self.clock.elapsed(), 3.0)

    def test_a_short_attempt_at_the_end_of_the_deadline_is_not_the_dependencys_failure(self):
        pol = Policy("d", **only(deadline={"timeout_s": 2.5}, retry={"base_s": 0}, attempt_timeout={"timeout_s": 2.0}, circuit_breaker={"fail_threshold": 2}))
        for _ in range(3):  # each call: one whole attempt, one of 0.5 s
            d = Dep(forever=10)
            self.assertRaises(TimeoutError, pol.call, d)
            pol.call(Dep())
        self.assertEqual(pol.layer("circuit_breaker").breaker.state, "closed")
        d = Dep(10, 10)
        with self.assertRaises(DeadlineExceeded):
            pol.call(d)
        self.assertEqual([a.timeout_s for a in d.attempts], [2.0, 0.5])
        with deadline(1.2):  # more than half a timeout counts
            self.assertRaises(CircuitOpenError, Policy("d", **only(retry={"base_s": 0}, attempt_timeout={"timeout_s": 0.4}, circuit_breaker={"fail_threshold": 2})).call,
                              Dep(forever=10))

    def test_standalone(self):
        self.assertIsNone(attempt_timeout())
        self.assertEqual(attempt_timeout(3), 3)
        with deadline(2):
            time.sleep(0.5)
            self.assertEqual((attempt_timeout(3), attempt_timeout(1), attempt_timeout()), (1.5, 1, 1.5))
            time.sleep(2)
            self.assertRaises(DeadlineExceeded, attempt_timeout, 1)


class DeadlineTest(VirtualCase):
    def test_nested_policies_inherit_the_earlier_deadline(self):
        inner, outer = Policy("b", **only(deadline={"timeout_s": 10.0}, attempt_timeout={"timeout_s": 10.0})), \
            Policy("a", **only(deadline={"timeout_s": 1.5}))
        d = Dep(30)
        self.assertRaises(TimeoutError, outer.call, lambda a: inner.call(d))
        self.assertEqual(d.attempts[0].timeout_s, 1.5)
        self.assertEqual(self.clock.elapsed(), 1.5)

    def test_attempt_timeout_limits_the_attempts_own_nested_calls(self):
        inner = Policy("b", **only(deadline=True, attempt_timeout={"timeout_s": 10.0}))
        outer = Policy("a", **only(deadline={"timeout_s": 4.0}, attempt_timeout={"timeout_s": 1.0}))
        d = Dep(30)
        self.assertRaises(TimeoutError, outer.call, lambda a: inner.call(d))
        self.assertEqual(d.attempts[0].timeout_s, 1.0)

    def test_nothing_is_sent_after_the_caller_gave_up(self):
        d = Dep(forever="x")
        with deadline(1):
            time.sleep(1)
            self.assertRaises(DeadlineExceeded, Policy("d").call, d)
        self.assertEqual(d.attempts, [])

    def test_travels_into_asyncio_tasks(self):
        async def main():
            with deadline(2):
                async def child():
                    await asyncio.sleep(0.5)
                    return remaining_s()
                return await asyncio.create_task(child())
        self.assertAlmostEqual(asyncio.run(main()), 1.5)

    def test_retry_and_its_budget_set_one_too(self):
        seen = []

        def fn():
            seen.append(remaining_s())
            time.sleep(attempt_timeout(1.0))
            raise TimeoutError()
        self.assertRaises(TimeoutError, retry, fn, attempts=9, base=0.0, budget_s=2.5)
        self.assertEqual(seen, [2.5, 1.5, 0.5])
        self.assertEqual(self.clock.elapsed(), 2.5)
        self.assertIsNone(remaining_s())

    def test_a_deadline_alone_cannot_cut_a_replayed_call(self):
        """Only what cuts a call in production may cut it in verify."""
        with deadline(1):
            self.assertIsNone(runtime.enforced_remaining_s())
        self.assertEqual(runtime.call_with_timeout(runtime.enforced_remaining_s, 2.0), 2.0)


class RetryTest(VirtualCase):
    def test_transient_errors_only_up_to_the_limit(self):
        pol = Policy("d", **only(retry={"attempts": 3}))
        d = Dep(DOWN, http_error(503))
        self.assertEqual((pol.call(d), [a.n for a in d.attempts]), ("ok", [1, 2, 3]))
        for e, n in ((DOWN, 3), (http_error(404), 1), (ValueError("bug"), 1)):
            d = Dep(forever=e)
            with self.assertRaises(type(e)):
                pol.call(d)
            self.assertEqual(len(d.attempts), n)

    def test_full_jitter(self):
        pol = Policy("d", **only(retry={"attempts": 6, "base_s": 1.0, "cap_s": 4.0}))
        waits = []
        for seed in range(50):
            runtime.seed(seed)
            d = Dep(forever=DOWN)
            self.assertRaises(ConnectionRefusedError, pol.call, d)
            waits.append([b - a for a, b in zip(d.at, d.at[1:])])
        for w in waits:
            for x, top in zip(w, (1, 2, 4, 4, 4)):
                self.assertTrue(0 <= x <= top)
        first = [w[0] for w in waits]
        self.assertTrue(min(first) < 0.2 and max(first) > 0.8 and 0.35 < sum(first) / 50 < 0.65)  # uniform over the range
        runtime.seed(3)
        d = Dep(DOWN)
        Policy("d", **only(retry={"base_s": 1.0, "full_jitter": False})).call(d)
        self.assertEqual(d.at[1] - d.at[0], 1.0)

    def test_no_wait_crosses_the_deadline(self):
        pol = Policy("d", **only(deadline={"timeout_s": 1.0}, retry={"base_s": 3.0, "full_jitter": False}))
        d = Dep(forever=DOWN)
        self.assertRaises(ConnectionRefusedError, pol.call, d)
        self.assertEqual((len(d.attempts), self.clock.elapsed()), (1, 0))

    def test_retry_helper_respects_a_surrounding_deadline(self):
        d = Dep(forever=http_error(503, retry_after=5))
        with deadline(2):
            self.assertRaises(urllib.error.HTTPError, retry, d)
        self.assertEqual(len(d.attempts), 1)

    def test_writes_are_repeated_only_with_a_key(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            d = Dep(forever=DOWN)
            self.assertRaises(ConnectionRefusedError, Policy("d").call, d, method=method)
            self.assertEqual(len(d.attempts), 1)
        d = Dep(DOWN)
        Policy("d").call(d, method="POST", idempotency_key="k")
        self.assertEqual(len(d.attempts), 2)

    def test_async(self):
        d = Dep(DOWN, DOWN)
        self.assertEqual(asyncio.run(Policy("d").acall(d.acall)), "ok")
        self.assertEqual(len(d.attempts), 3)


class RetryBudgetTest(VirtualCase):
    def test_bound_holds_whatever_happens(self):
        """retries <= burst + ratio x requests + min_per_s x seconds, checked after every step of a random story."""
        import random
        rnd = random.Random(5)
        b = RetryBudget(ratio=0.2, min_per_s=0.5, burst=4)
        requests = retries = 0
        for _ in range(5000):
            x = rnd.random()
            if x < 0.4:
                b.record_request()
                requests += 1
            elif x < 0.8:
                retries += b.try_spend()
            else:
                time.sleep(rnd.random())
            self.assertLessEqual(retries, 4 + 0.2 * requests + 0.5 * self.clock.elapsed() + 1e-6)
        self.assertGreater(retries, 0.2 * requests)  # and it isn't simply "no"

    def test_never_holds_more_than_burst_and_resets(self):
        b = RetryBudget(burst=3, min_per_s=100)
        time.sleep(1000)
        self.assertEqual(b.tokens, 3)
        self.assertEqual([b.try_spend() for _ in range(5)], [True, True, True, False, False])
        runtime.reset_state()
        self.assertEqual(b.tokens, 3)

    def test_outage_amplification_is_bounded(self):
        def attempts(**on):
            pol, d = Policy("d", **only(retry={"attempts": 4, "base_s": 0}, **on)), Dep(forever=DOWN)
            for _ in range(200):
                self.assertRaises(ConnectionRefusedError, pol.call, d)
            return len(d.attempts)
        self.assertEqual(attempts(), 800)
        self.assertLessEqual(attempts(retry_budget={"ratio": 0.1, "min_per_s": 0, "burst": 10}), 200 + 10 + 20)

    def test_a_retry_the_server_asked_for_is_free(self):
        none = {"ratio": 0.0, "min_per_s": 0.0, "burst": 0.0}
        for on, attempts in ((dict(retry_after=True), 2), ({}, 1)):
            d = Dep(http_error(429, retry_after=1))
            with contextlib.suppress(urllib.error.HTTPError):
                Policy("d", **only(retry=True, retry_budget=none, **on)).call(d)
            self.assertEqual(len(d.attempts), attempts)
        d = Dep(http_error(503, retry_after=1), DOWN)
        self.assertRaises(ConnectionRefusedError, Policy("d", **only(retry=True, retry_after=True, retry_budget=none)).call, d)
        self.assertEqual(len(d.attempts), 2)  # the second failure named no time: that retry needs a token
        d = Dep(http_error(429, retry_after=1))
        self.assertEqual(retry(d, retry_budget=RetryBudget(**none)), "ok")

    def test_shared_by_calls_of_the_retry_helper(self):
        b = RetryBudget(ratio=0, min_per_s=0, burst=3)
        d = Dep(forever=DOWN)
        for _ in range(5):
            self.assertRaises(ConnectionRefusedError, retry, d, attempts=3, base=0, retry_budget=b)
        self.assertEqual(len(d.attempts), 5 + 3)

    def test_threads(self):
        b = RetryBudget(ratio=0, min_per_s=0, burst=100)
        got = []
        ts = [threading.Thread(target=lambda: got.extend(b.try_spend() for _ in range(50))) for _ in range(8)]
        [t.start() for t in ts], [t.join() for t in ts]
        self.assertEqual(sum(got), 100)


class RetryAfterTest(VirtualCase):
    def test_waits_as_long_as_the_server_asked(self):
        pol = Policy("d", **only(retry_after=True, retry={"base_s": 0}))
        for code in (429, 503):
            d = Dep(http_error(code, retry_after=2))
            pol.call(d)
            self.assertEqual(d.at[1] - d.at[0], 2.0)
        d = Dep(http_error(429, retry_after=2))
        Policy("d", **only(retry={"base_s": 0})).call(d)  # off: comes back at once
        self.assertEqual(d.at[1], d.at[0])

    def test_gives_up_at_once_when_that_is_too_late(self):
        pol = Policy("d", **only(retry_after=True, retry=True, deadline={"timeout_s": 4.0}))
        d = Dep(http_error(429, retry_after=30))
        self.assertRaises(urllib.error.HTTPError, pol.call, d)
        self.assertEqual((len(d.attempts), self.clock.elapsed()), (1, 0))

    def test_http_dates_and_nonsense(self):
        from email.utils import formatdate
        self.assertAlmostEqual(runtime.retry_after_s(http_error(503, formatdate(time.time() + 60, usegmt=True))), 60, delta=1.5)
        self.assertEqual(runtime.retry_after_s(http_error(503, -5)), 0.0)
        for e in (http_error(503, "soon"), http_error(503), ValueError()):
            self.assertIsNone(runtime.retry_after_s(e))


class CircuitBreakerTest(VirtualCase):
    def fail(self, cb, n=1, e=DOWN):
        for _ in range(n):
            with self.assertRaises(type(e)):
                cb.call(Dep(e))

    def test_consecutive_failures(self):
        cb = CircuitBreaker(fail_threshold=3, cooldown_s=10)
        self.fail(cb, 2)
        cb.call(Dep())
        self.fail(cb, 2)
        self.assertEqual(cb.state, "closed")
        self.fail(cb)
        self.assertEqual(cb.state, "open")
        d = Dep()
        self.assertRaises(CircuitOpenError, cb.call, d)
        self.assertEqual(d.attempts, [])

    def test_failure_rate_window(self):
        cb = CircuitBreaker(fail_threshold=None, failure_rate=0.5, window=10, min_calls=8)
        for _ in range(4):
            cb.call(Dep())
        self.fail(cb, 3)
        self.assertEqual(cb.state, "closed")  # 3 of 7: too few calls to judge
        self.fail(cb)
        self.assertEqual(cb.state, "open")    # 4 of 8
        cb = CircuitBreaker(fail_threshold=None, failure_rate=0.5, window=10, min_calls=8)
        self.fail(cb, 1)
        for _ in range(60):                   # old failures leave the window
            cb.call(Dep())
            self.assertEqual(cb.state, "closed")
        self.fail(cb, 4)
        self.assertEqual(cb.state, "closed")

    def test_half_open_probe_count(self):
        cb = CircuitBreaker(fail_threshold=1, cooldown_s=5, half_open_probes=2)
        self.fail(cb)
        time.sleep(5)
        self.assertEqual(cb.state, "half_open")
        a, b = cb.acquire(), cb.acquire()
        self.assertRaises(CircuitOpenError, cb.acquire)
        cb.release(a)
        self.assertEqual(cb.state, "half_open")
        cb.release(b)
        self.assertEqual(cb.state, "closed")

    def test_failed_probe_reopens_for_a_whole_cooldown(self):
        cb = CircuitBreaker(fail_threshold=1, cooldown_s=5)
        self.fail(cb)
        time.sleep(5)
        self.fail(cb)
        time.sleep(4.9)
        self.assertEqual(cb.state, "open")
        time.sleep(0.2)
        cb.call(Dep())
        self.assertEqual(cb.state, "closed")

    def test_other_errors_are_neutral_and_free_the_probe(self):
        cb = CircuitBreaker(fail_threshold=1, cooldown_s=5)
        self.fail(cb, 3, KeyError("bug"))
        self.fail(cb, 3, http_error(404))
        self.assertEqual(cb.state, "closed")
        self.fail(cb)
        time.sleep(5)
        self.fail(cb, 1, KeyError("bug"))
        with self.assertRaises(KeyboardInterrupt):  # also when the probe is cancelled
            cb.call(Dep(KeyboardInterrupt()))
        self.assertEqual(cb.state, "half_open")
        cb.call(Dep())
        self.assertEqual(cb.state, "closed")

    def test_late_outcomes_of_older_calls_do_not_count(self):
        cb = CircuitBreaker(fail_threshold=1, cooldown_s=5)
        old = cb.acquire()
        self.fail(cb)
        time.sleep(5)
        probe = cb.acquire()
        cb.release(old, DOWN)
        self.assertEqual(cb.state, "half_open")
        cb.release(probe)
        self.assertEqual(cb.state, "closed")

    def test_a_429_is_not_an_outage(self):
        cb = CircuitBreaker(fail_threshold=1)
        self.fail(cb, 5, http_error(429, 1))
        self.assertEqual(cb.state, "closed")
        self.fail(cb, 1, http_error(503))
        self.assertEqual(cb.state, "open")

    def test_own_deadline_is_not_the_dependencys_failure(self):
        cb = CircuitBreaker(fail_threshold=1)
        self.fail(cb, 3, DeadlineExceeded("ours"))
        self.assertEqual(cb.state, "closed")

    def test_in_a_policy_the_open_circuit_is_not_retried(self):
        pol = Policy("d", **only(retry={"attempts": 4, "base_s": 0}, circuit_breaker={"fail_threshold": 2}))
        d = Dep(forever=DOWN)
        self.assertRaises(CircuitOpenError, pol.call, d)
        self.assertRaises(CircuitOpenError, pol.call, d)
        self.assertEqual(len(d.attempts), 2)

    def test_async(self):
        cb = CircuitBreaker(fail_threshold=1)
        with self.assertRaises(ConnectionRefusedError):
            asyncio.run(cb.acall(Dep(DOWN).acall, None))
        with self.assertRaises(CircuitOpenError):
            asyncio.run(cb.acall(Dep().acall, None))


class BulkheadTest(VirtualCase):
    def test_rejects_fast_when_full_and_frees_slots(self):
        pol = Policy("d", **only(bulkhead={"max_concurrent": 2}))
        d = Dep(1, 1)

        async def main():
            first = await asyncio.gather(*(pol.acall(d.acall) for _ in range(5)), return_exceptions=True)
            return first, time.monotonic(), await pol.acall(d.acall)
        first, at, again = asyncio.run(main())
        self.assertEqual([type(x).__name__ for x in first], ["str"] * 2 + ["BulkheadFullError"] * 3)
        self.assertEqual((again, at), ("ok", d.at[0] + 1))
        self.assertRaises(ConnectionRefusedError, pol.call, Dep(DOWN))
        self.assertEqual(pol.layer("bulkhead").bulkhead.in_use, 0)

    def test_standalone_and_nested_calls_cannot_deadlock(self):
        b = Bulkhead(1, "db")
        with b.slot():
            self.assertRaisesRegex(BulkheadFullError, "bulkhead full for db", b.call, lambda: 1)
        self.assertEqual(b.call(lambda: 1), 1)
        with self.assertRaises(ValueError):
            b.call(Dep(ValueError()))
        self.assertEqual(b.in_use, 0)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(b.acall(Dep(asyncio.CancelledError()).acall))
        self.assertEqual(b.in_use, 0)

    def test_one_slow_dependency_does_not_block_another(self):
        a, b = Policy("a", **only(bulkhead={"max_concurrent": 1})), Policy("b", **only(bulkhead={"max_concurrent": 1}))
        self.assertEqual(a.call(lambda _: b.call(lambda _: "ok")), "ok")


class AdaptiveThrottleTest(VirtualCase):
    def test_the_sre_formula(self):
        t = AdaptiveThrottle(k=2.0, window_s=120)
        for _ in range(10):
            t.call(Dep())
        self.assertEqual(t.reject_probability(), 0.0)
        runtime._rng.seed(0)
        for _ in range(30):
            with self.assertRaises((ConnectionRefusedError, ThrottledError)):
                t.call(Dep(DOWN))
        self.assertEqual((t.requests, t.accepts), (40, 10))  # locally rejected ones count as requests
        self.assertAlmostEqual(t.reject_probability(), (40 - 2 * 10) / 41)

    def test_forgets_after_the_window(self):
        t = AdaptiveThrottle(window_s=120)
        for _ in range(20):
            with self.assertRaises((ConnectionRefusedError, ThrottledError)):
                t.call(Dep(DOWN))
        self.assertGreater(t.reject_probability(), 0.9)
        time.sleep(121)
        self.assertEqual((t.reject_probability(), t.requests), (0.0, 0))

    def test_sheds_most_load_in_an_outage_and_keeps_probing(self):
        t, d = AdaptiveThrottle(), Dep(forever=DOWN)
        for _ in range(500):
            time.sleep(0.02)
            with self.assertRaises((ConnectionRefusedError, ThrottledError)):
                t.call(d)
        self.assertLess(len(d.attempts), 50)
        self.assertGreater(len(d.attempts), 1)

    def test_a_burst_in_flight_is_no_rejection_and_a_429_is(self):
        t = AdaptiveThrottle()
        for _ in range(100):
            t.acquire()
        self.assertEqual(t.reject_probability(), 0.0)
        for _ in range(100):
            t.release(http_error(429))
        self.assertGreater(t.reject_probability(), 0.9)

    def test_an_answer_is_an_accept_even_if_it_is_a_404(self):
        t = AdaptiveThrottle()
        for _ in range(50):
            with self.assertRaises(urllib.error.HTTPError):
                t.call(Dep(http_error(404)))
        self.assertEqual(t.reject_probability(), 0.0)


class AdaptiveConcurrencyTest(VirtualCase):
    def test_additive_increase_multiplicative_decrease(self):
        c = AdaptiveConcurrency(initial=4, min_limit=1, max_limit=6, backoff=0.5)
        for _ in range(40):
            for _ in range(4):
                c.acquire()
            for _ in range(4):
                c.release()
        self.assertEqual(c.limit, 6)
        c.acquire()
        c.release(DOWN)
        self.assertEqual(c.limit, 3)
        for _ in range(100):
            c.acquire()
            c.release(DOWN)
        self.assertEqual(c.limit, 1)
        c.acquire()
        self.assertRaises(ConcurrencyLimitError, c.acquire)
        c.release()

    def test_errors_of_ours_do_not_shrink_it(self):
        c = AdaptiveConcurrency(initial=4)
        for e in (KeyError(), http_error(404), DeadlineExceeded()):
            with self.assertRaises(type(e)):
                c.call(Dep(e))
        self.assertEqual((c.limit, c.in_flight), (4, 0))


class HedgeTest(VirtualCase):
    def both(self, *script, **kw):
        """(result or error, seconds, attempts) from the virtual-time race and from asyncio: they must agree."""
        out = []
        for style in ("sync", "async"):
            d, t0 = Dep(*script), self.clock.t
            try:
                r = hedge(d, **kw) if style == "sync" else asyncio.run(ahedge(d.acall, **kw))
            except Exception as e:
                r = type(e).__name__
            out.append((r, round(self.clock.t - t0, 6), [round(x - t0, 6) for x in d.at], d.attempts))
        self.assertEqual(out[0][:3], out[1][:3])
        return out[1]

    def test_a_fast_answer_is_not_hedged(self):
        self.assertEqual(self.both(0.5, delay_s=1.0)[:3], ("ok", 0.5, [0]))

    def test_a_slow_one_is_and_the_first_answer_wins(self):
        r, took, at, attempts = self.both(10, 0.5, delay_s=1.0)
        self.assertEqual((r, took, at), ("ok", 1.5, [0, 1]))
        self.assertTrue(attempts[0].cancelled)
        self.assertEqual(self.both(2, 10, delay_s=1.0)[:3], ("ok", 2.0, [0, 1]))

    def test_errors(self):
        self.assertEqual(self.both(DOWN, delay_s=1.0)[:3], ("ConnectionRefusedError", 0, [0]))      # before the hedge: not its business
        self.assertEqual(self.both(3, DOWN, delay_s=1.0)[:3], ("ok", 3, [0, 1]))                      # the hedge failed, the first answers

    def test_several_hedges_and_budget(self):
        self.assertEqual(self.both(10, 10, 0.5, delay_s=1.0, max_hedges=2)[:3], ("ok", 2.5, [0, 1, 2]))
        self.assertEqual(self.both(3, 0.1, delay_s=1.0, retry_budget=RetryBudget(burst=0, min_per_s=0))[:3], ("ok", 3, [0]))

    def test_reads_only(self):
        for m in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertRaises(ValueError, hedge, Dep(), delay_s=1, method=m)
            d = Dep(5)
            Policy("d", **only(hedge=True, idempotency=True)).call(d, method=m, idempotency_key="k")
            self.assertEqual(len(d.attempts), 1)

    def test_the_loser_is_cancelled(self):
        ended = []

        async def fn(a):
            try:
                await asyncio.sleep(10 if a.n == 1 else 0.1)
                return a.n
            except asyncio.CancelledError:
                ended.append(a.n)
                raise
        self.assertEqual(asyncio.run(ahedge(fn, delay_s=1)), 2)
        self.assertEqual(ended, [1])

    def test_cancelling_the_hedged_call_cancels_every_branch(self):
        ended = []

        async def fn(a):
            try:
                await asyncio.sleep(100)
            finally:
                ended.append(a.n)

        async def main():
            with self.assertRaises((asyncio.TimeoutError, TimeoutError)):
                await asyncio.wait_for(ahedge(fn, delay_s=1), 3)
        asyncio.run(main())
        self.assertEqual(sorted(ended), [1, 2])

    def test_policy_learns_when_to_hedge(self):
        pol = Policy("d", **only(hedge={"delay_s": 5.0, "quantile": 0.95}))
        d = Dep(*[0.2] * 30, 3, 0.2)
        for _ in range(31):
            t0 = time.monotonic()
            pol.call(d)
        self.assertAlmostEqual(time.monotonic() - t0, 0.4)
        self.assertEqual(len(d.attempts), 32)

    def test_real_threads(self):
        self.clock.uninstall()
        runtime.VIRTUAL_CLOCK = None
        seen = []

        def fn(a):
            seen.append(a)
            time.sleep(0.6 if a.n == 1 else 0.01)
            return a.n
        t0 = time.perf_counter()
        self.assertEqual(hedge(fn, delay_s=0.05), 2)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertTrue(seen[0].cancelled)
        self.assertRaises(ConnectionRefusedError, hedge, Dep(DOWN), delay_s=0.5)


class FallbackTest(VirtualCase):
    def test_fresh_then_stale_then_degraded_then_error(self):
        pol = Policy("d", **only(fallback={"stale_ttl_s": 60.0, "degraded": {"status": "unavailable"}}))
        self.assertEqual(pol.call(lambda a: {"v": 1}, cache_key="k"), {"v": 1})
        time.sleep(59)
        down = Dep(forever=DOWN)
        self.assertEqual(pol.call(down, cache_key="k"), {"v": 1})
        self.assertEqual(pol.call(down, cache_key="other"), {"status": "unavailable"})
        self.assertEqual(pol.call(down), {"status": "unavailable"})
        time.sleep(2)
        self.assertEqual(pol.call(down, cache_key="k"), {"status": "unavailable"})  # too old now
        self.assertRaises(ConnectionRefusedError, Policy("d", **only(fallback=True)).call, down, cache_key="k")
        self.assertEqual([x["Attributes"]["level"] for x in self.logged()], ["stale"] + ["degraded"] * 3)
        self.assertEqual(self.logged()[0]["Attributes"]["age_s"], 59.0)
        self.assertTrue(all(len(x["trace_id"]) == 32 for x in self.logged()))

    def test_the_dependency_is_always_asked_first(self):
        pol, d = Policy("d", **only(fallback=True)), Dep()
        for _ in range(3):
            pol.call(d, cache_key="k")
        self.assertEqual(len(d.attempts), 3)

    def test_bugs_and_permanent_errors_are_not_hidden(self):
        pol = Policy("d", **only(fallback={"degraded": 0}))
        for e in (KeyError("bug"), http_error(404), http_error(400)):
            with self.assertRaises(type(e)):
                pol.call(Dep(e))
        self.assertEqual(self.logged(), [])

    def test_open_circuit_steps_down_even_with_a_custom_rule(self):
        pol = Policy("d", **only(circuit_breaker={"fail_threshold": 1}, fallback={"degraded": lambda e: type(e).__name__, "when": lambda e: e is DOWN}))
        self.assertEqual([pol.call(Dep(DOWN)), pol.call(Dep(DOWN))], ["ConnectionRefusedError", "CircuitOpenError"])

    def test_cache_hands_out_copies_and_is_bounded(self):
        c = StaleCache(ttl_s=10, max_entries=2)
        v = {"a": [1]}
        c.put("x", v)
        v["a"].append(2)
        c.get("x")[0]["a"].append(3)
        self.assertEqual(c.get("x")[0], {"a": [1]})
        c.put("y", 1), c.put("z", 1)
        self.assertEqual((c.get("x"), c.get("z")), (None, (1, 0.0)))
        c.put("lock", threading.Lock())  # can't be copied: not kept
        self.assertIsNone(c.get("lock"))


class IdempotencyTest(VirtualCase):
    def test_one_key_in_every_attempt(self):
        pol, d = Policy("d"), Dep(DOWN, DOWN)
        pol.call(d, method="POST", idempotency_key=idempotency_key("charge", "INV-1", 1500))
        self.assertEqual(len({a.idempotency_key for a in d.attempts}), 1)
        self.assertEqual(len({a.headers["Idempotency-Key"] for a in d.attempts}), 1)
        self.assertRegex(d.attempts[0].headers["Idempotency-Key"], r'^"[0-9a-f-]{36}"$')  # a structured-field string
        bare, d = Policy("d", idempotency={"quoted": False}), Dep()
        bare.call(d, method="POST", idempotency_key="k")
        self.assertEqual(d.attempts[0].headers, {"Idempotency-Key": "k"})

    def test_key_names_the_operation(self):
        keys = {idempotency_key(*p) for p in (("charge", "1"), ("charge", 1), ("charge", "1", None), ("charge",), ("refund", "1"), ("charge", True), ("charge", "1,2"), ("charge", "1", "2"))}
        self.assertEqual(len(keys), 8)
        for k in keys:
            self.assertRegex(k, r"^[0-9a-f]{8}-[0-9a-f]{4}-8[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        for bad in (1.5, object(), ["x"], time):
            self.assertRaises(TypeError, idempotency_key, "x", bad)

    def test_409_is_asked_again_and_422_is_not(self):
        d = Dep(http_error(409))
        self.assertEqual(Policy("d").call(d, method="POST", idempotency_key="k"), "ok")
        self.assertEqual(len({a.idempotency_key for a in d.attempts}), 1)
        for code, kw in ((422, {"method": "POST", "idempotency_key": "k"}), (409, {"method": "POST"}), (409, {})):
            d = Dep(forever=http_error(code))
            self.assertRaises(urllib.error.HTTPError, Policy("d").call, d, **kw)
            self.assertEqual(len(d.attempts), 1)
        self.assertTrue(runtime.is_idempotency_conflict(http_error(409)) and runtime.is_idempotency_mismatch(http_error(422)))

    def test_off_means_writes_are_not_repeated(self):
        d = Dep(DOWN)
        self.assertRaises(ConnectionRefusedError, Policy("d", idempotency=False).call, d, method="POST", idempotency_key="k")
        self.assertEqual((len(d.attempts), d.attempts[0].headers), (1, {}))


class PolicyTest(VirtualCase):
    def test_everything_together_stays_in_the_deadline(self):
        pol = Policy("d", **{n: True for n in patterns.NAMES})
        for seed in range(20):
            runtime.seed(seed)
            runtime.reset_state()
            t0 = self.clock.t
            with self.assertRaises((TimeoutError, RejectedError)):
                pol.call(Dep(forever=60))
            self.assertLessEqual(self.clock.t - t0, 4.8 + 1e-6)

    def test_state_is_per_policy(self):
        a, b = Policy("a", **only(circuit_breaker={"fail_threshold": 1})), Policy("b", **only(circuit_breaker={"fail_threshold": 1}))
        self.assertRaises(ConnectionRefusedError, a.call, Dep(DOWN))
        self.assertRaises(CircuitOpenError, a.call, Dep())
        self.assertEqual(b.call(Dep()), "ok")

    def test_reset_state_forgets_everything(self):
        pol = Policy("d", **only(circuit_breaker={"fail_threshold": 1}, fallback={"stale_ttl_s": 1e9}))
        pol.call(lambda a: 1, cache_key="k")
        pol.call(Dep(DOWN), cache_key="k")
        runtime.reset_state()
        self.assertRaises(ConnectionRefusedError, pol.call, Dep(DOWN), cache_key="k")

    def test_call_tells_reads_from_writes(self):
        for m, safe in (("GET", True), ("head", True), ("RUN", True), ("POST", False), ("DELETE", False)):
            self.assertEqual(Call("d", method=m).safe, safe)


if __name__ == "__main__":
    unittest.main()
