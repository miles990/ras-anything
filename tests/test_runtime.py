import asyncio
import io
import json
import sys
import unittest
import urllib.error
from email.message import Message
from unittest import mock

from ras_anything import runtime
from ras_anything.runtime import CircuitBreaker, CircuitOpenError, RasError, call_with_timeout, is_transient, retry


def http_error(code, retry_after=None):
    h = Message()
    if retry_after is not None:
        h["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("http://x", code, "err", h, io.BytesIO(b""))


class Flaky:
    def __init__(self, errors, value="ok"):
        self.errors, self.value, self.calls = list(errors), value, 0

    def __call__(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.value


class AsyncRetryTest(unittest.TestCase):
    def run_flaky(self, errors, **kw):
        f = Flaky(errors)

        async def call():
            return f()

        return f, asyncio.run(runtime.aretry(call, base=0.001, cap=0.002, **kw))

    def test_same_rules_as_retry(self):
        f, out = self.run_flaky([TimeoutError(), http_error(503)], attempts=3)
        self.assertEqual((out, f.calls), ("ok", 3))
        with self.assertRaises(urllib.error.HTTPError):
            self.run_flaky([http_error(404)], attempts=3)
        with self.assertRaises(TimeoutError):
            self.run_flaky([TimeoutError()] * 3, attempts=3)
        with self.assertRaises(urllib.error.HTTPError):
            self.run_flaky([http_error(429, retry_after=9)], attempts=3, budget_s=1)

    def test_transient_errors_of_optional_clients(self):
        try:
            import httpx
            import aiohttp
        except ImportError:
            self.skipTest("httpx/aiohttp not installed")
        req = httpx.Request("GET", "http://x")
        for e, yes in ((httpx.ReadTimeout("t"), True), (httpx.ConnectError("c"), True), (httpx.RemoteProtocolError("p"), True),
                       (httpx.InvalidURL("u"), False), (aiohttp.ServerDisconnectedError(), True), (aiohttp.ClientPayloadError(), True),
                       (aiohttp.InvalidURL("u"), False), (aiohttp.ClientResponseError(None, (), status=503), True),
                       (aiohttp.ClientResponseError(None, (), status=404), False),
                       (httpx.HTTPStatusError("s", request=req, response=httpx.Response(502, request=req)), True),
                       (httpx.HTTPStatusError("s", request=req, response=httpx.Response(403, request=req)), False)):
            self.assertEqual(is_transient(e), yes, repr(e))


class RetryTest(unittest.TestCase):
    def setUp(self):
        runtime.seed(7)
        self.sleeps = []

    def test_retries_transient_then_succeeds(self):
        f = Flaky([TimeoutError(), ConnectionRefusedError(), http_error(503)])
        self.assertEqual(retry(f, attempts=4, base=0.1, cap=1, sleep=self.sleeps.append), "ok")
        self.assertEqual(f.calls, 4)
        self.assertEqual(len(self.sleeps), 3)

    def test_full_jitter_bounds(self):
        f = Flaky([TimeoutError()] * 5)
        retry(f, attempts=6, base=0.1, cap=0.3, sleep=self.sleeps.append)
        for n, s in enumerate(self.sleeps):
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(s, min(0.3, 0.1 * 2 ** n))

    def test_never_retries_4xx(self):
        f = Flaky([http_error(404)])
        with self.assertRaises(urllib.error.HTTPError):
            retry(f, attempts=5, sleep=self.sleeps.append)
        self.assertEqual(f.calls, 1)

    def test_honours_retry_after(self):
        f = Flaky([http_error(429, retry_after=2)])
        retry(f, attempts=2, base=0.01, cap=0.01, sleep=self.sleeps.append)
        self.assertEqual(self.sleeps, [2.0])

    def test_budget_stops_retries_and_reraises_same_type(self):
        f = Flaky([http_error(429, retry_after=10)] * 3)
        with self.assertRaises(urllib.error.HTTPError):
            retry(f, attempts=3, budget_s=1.0, sleep=self.sleeps.append)
        self.assertEqual(f.calls, 1)
        self.assertEqual(self.sleeps, [])

    def test_retry_on_types(self):
        f = Flaky([ValueError("bad json")])
        self.assertEqual(retry(f, attempts=2, retry_on=(ValueError,), sleep=self.sleeps.append), "ok")

    def test_seeded_jitter_is_deterministic(self):
        def run():
            runtime.seed(3)
            s = []
            retry(Flaky([TimeoutError()] * 3), attempts=4, sleep=s.append)
            return s
        self.assertEqual(run(), run())

    def test_is_transient(self):
        self.assertTrue(is_transient(http_error(502)))
        self.assertTrue(is_transient(urllib.error.URLError(ConnectionRefusedError())))
        self.assertTrue(is_transient(BlockingIOError(11, "Resource temporarily unavailable")))
        self.assertFalse(is_transient(http_error(400)))
        self.assertFalse(is_transient(KeyError("x")))


class BreakerTest(unittest.TestCase):
    def test_open_half_open_close(self):
        now = [100.0]
        with mock.patch("time.monotonic", lambda: now[0]):
            cb = CircuitBreaker(fail_threshold=2, cooldown_s=10)
            for _ in range(2):
                with self.assertRaises(TimeoutError):
                    cb.call(Flaky([TimeoutError()]))
            self.assertEqual(cb.state, "open")
            with self.assertRaises(CircuitOpenError):
                cb.call(lambda: "never")
            now[0] += 10
            self.assertEqual(cb.state, "half_open")
            with self.assertRaises(TimeoutError):  # failed trial re-opens
                cb.call(Flaky([TimeoutError()]))
            self.assertEqual(cb.state, "open")
            now[0] += 10
            self.assertEqual(cb.call(lambda: 1), 1)
            self.assertEqual(cb.state, "closed")

    def test_non_transient_does_not_count(self):
        cb = CircuitBreaker(fail_threshold=1)
        with self.assertRaises(KeyError):
            cb.call(Flaky([KeyError()]))
        self.assertEqual(cb.state, "closed")

    def test_half_open_admits_single_probe(self):
        import threading
        now = [0.0]
        with mock.patch("ras_anything.runtime._now", side_effect=lambda: now[0]):
            cb = CircuitBreaker(fail_threshold=1, cooldown_s=5.0)
            with self.assertRaises(TimeoutError):
                cb.call(Flaky([TimeoutError()]))
            now[0] += 5.0
            entered = threading.Event()
            release = threading.Event()

            def probe():
                entered.set()
                release.wait(2.0)
                return "ok"

            out = {}
            t = threading.Thread(target=lambda: out.setdefault("v", cb.call(probe)))
            t.start()
            entered.wait(2.0)
            with self.assertRaises(CircuitOpenError):
                cb.call(lambda: "second")
            release.set()
            t.join(2.0)
            self.assertEqual(out.get("v"), "ok")
            self.assertEqual(cb.state, "closed")


class ErrorsAndLogsTest(unittest.TestCase):
    def test_ras_error(self):
        cause = TimeoutError("slow")
        e = RasError("fetch failed", cause=cause, context={"city": "Lima"})
        self.assertIs(e.__cause__, cause)
        self.assertNotIn("Lima", str(e))  # context is for our logs, str() is for anyone
        self.assertEqual(len(e.trace_id), 32)
        self.assertEqual(e.to_dict()["context"], {"city": "Lima"})

    def test_log_event_json_line(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            runtime.log_event("gave_up", city="Lima", attempts=3)
        rec = json.loads(buf.getvalue())
        self.assertEqual((rec["event"], rec["Attributes"]), ("gave_up", {"city": "Lima", "attempts": 3}))
        self.assertIn("trace_id", rec)

    def test_call_with_timeout(self):
        import time
        self.assertEqual(call_with_timeout(lambda: 5, 1.0), 5)
        with self.assertRaises(TimeoutError):
            call_with_timeout(time.sleep, 0.05, 1.0)


if __name__ == "__main__":
    unittest.main()
