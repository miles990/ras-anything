"""Diagnostics (OpenTelemetry log records, W3C trace context), health, agent-readable errors, the human gate and the
flight recorder: the runtime of both languages."""

import asyncio
import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

from ras_anything import runtime as rt
from ras_anything.patterns import problem as ax
from ras_anything.patterns import recorder
from ras_anything.runtime import CircuitBreaker, CircuitOpenError, Policy, RasError, observed, retry, swap
from tests.node_support import AGENT, needs_node, run_js

DEP = [{"name": "charges", "kind": "http", "method": "POST", "route": "/v1/charges", "program": None}]


def http_error(code, retry_after=None, url="http://10.1.2.3:8080/v1/charges"):
    h = Message()
    if retry_after is not None:
        h["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(url, code, "err", h, io.BytesIO(b""))


class Failing:
    def __init__(self, *errors):
        self.errors = list(errors)

    def __call__(self, *_):
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


class Case(unittest.TestCase):
    def setUp(self):
        rt.seed(1)
        rt.reset_state()
        recorder.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"RAS_ANYTHING_INCIDENTS": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.err = io.StringIO()
        out = contextlib.redirect_stderr(self.err)
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)

    def records(self, name=None):
        rs = [json.loads(ln) for ln in self.err.getvalue().splitlines()]
        return [r for r in rs if name is None or r["EventName"] == name]

    def incidents(self):
        return [json.loads(p.read_text()) for p in sorted(Path(self.tmp.name).glob("inc-*.json"))]


def charge(order, *, fail=()):
    return retry(Failing(*fail), attempts=3, base=0.0)


class RecordsTest(Case):
    def test_log_data_model_and_correlation(self):
        f = observed("billing.charge", charge, DEP)
        with rt.trace("00-" + "a" * 32 + "-" + "b" * 16 + "-01"):
            f({"id": 1}, fail=[http_error(503), TimeoutError("timed out")])
        rs = self.records()
        self.assertEqual([r["EventName"] for r in rs], ["ras.dependency.failure", "ras.retry", "ras.dependency.failure", "ras.retry",
                                                        "ras.dependency.recovered"])
        for r in rs:
            self.assertEqual(r["TraceId"], "a" * 32)
            self.assertRegex(r["SpanId"], r"^[0-9a-f]{16}$")
            self.assertTrue(r["Timestamp"].isdigit() and len(r["Timestamp"]) == 19)
            self.assertEqual(r["SeverityNumber"], {"INFO": 9, "WARN": 13}[r["SeverityText"]])
            self.assertEqual(r["Attributes"]["code.function"], "billing.charge")
            self.assertIn("Body", r)
        self.assertEqual(len({r["SpanId"] for r in rs}), 1)
        a, b = rs[0]["Attributes"], rs[2]["Attributes"]
        self.assertEqual((a["ras.dependency"], a["server.address"], a["server.port"], a["error.type"], a["http.response.status_code"], a["ras.fault"]),
                         ("charges", "10.1.2.3", 8080, "503", 503, "unavailable"))
        self.assertEqual((b["ras.dependency"], b["url.template"], b["error.type"], b["ras.fault"], b["http.request.resend_count"]),
                         ("charges", "/v1/charges", "TimeoutError", "timeout", 1))

    def test_give_up_fallback_breaker_and_slow(self):
        pol = Policy("shop", retry={"attempts": 2, "base_s": 0.0}, circuit_breaker={"fail_threshold": 2}, fallback={"degraded": 7})
        f = observed("m.f", lambda: pol.call(Failing(*[ConnectionRefusedError()] * 9), method="POST", idempotency_key="k"), DEP)
        self.assertEqual(f(), 7)
        self.assertEqual(f(), 7)
        names = [r["EventName"] for r in self.records()]
        self.assertEqual(names.count("ras.dependency.gave_up"), 2)
        self.assertEqual(names.count("fallback"), 2)
        self.assertEqual(names.count("ras.breaker.transition"), 1)
        up, rejected = self.records("ras.dependency.gave_up")
        self.assertEqual((up["SeverityText"], up["Attributes"]["ras.fault"], up["Attributes"]["ras.next_action"], up["Attributes"]["ras.idempotency_key"]),
                         ("ERROR", "refused", "retry_later", "k"))
        self.assertEqual(rejected["Attributes"]["ras.fault"], "circuit_open")
        self.assertGreater(rejected["Attributes"]["ras.retry_after_s"], 0)
        self.err.truncate(0), self.err.seek(0)
        with mock.patch.dict(os.environ, {"RAS_ANYTHING_SLOW_S": "0"}):
            observed("m.f", lambda: retry(lambda: 1), DEP)()
        self.assertEqual([r["Attributes"]["ras.fault"] for r in self.records()], ["slow"])

    def test_the_boundary_calls_frame_names_a_dependency_when_there_are_several(self):
        import urllib.request
        deps = DEP + [{"name": "items", "kind": "http", "method": "GET", "route": "/v1/items", "program": None}]

        def get():
            return retry(lambda: urllib.request.urlopen("http://127.0.0.1:9/v1/items/7", timeout=2), attempts=1)
        with self.assertRaises(urllib.error.URLError):
            observed("m.get", get, deps)()
        a = self.records("ras.dependency.failure")[0]["Attributes"]
        self.assertEqual((a["ras.dependency"], a["server.address"], a["url.path"], a["ras.fault"]), ("items", "127.0.0.1", "/v1/items/7", "refused"))

    def test_levels_and_fail_safety(self):
        with mock.patch.dict(os.environ, {"RAS_ANYTHING_LOG": "error"}):
            observed("m.f", charge, DEP)(1, fail=[TimeoutError()])
        self.assertEqual(self.records(), [])
        with mock.patch("sys.stderr", None), mock.patch.object(rt, "_error_attributes", side_effect=RuntimeError):
            self.assertEqual(observed("m.f", charge, DEP)(1, fail=[TimeoutError()]), "ok")
        with mock.patch.object(rt, "CallContext", side_effect=RuntimeError):
            self.assertEqual(observed("m.f", charge, DEP)(1), "ok")
        with mock.patch.object(recorder, "finish", side_effect=RuntimeError):
            self.assertEqual(observed("m.f", charge, DEP)(1), "ok")

    def test_traceparent(self):
        for bad in (None, "", "00-" + "0" * 32 + "-" + "b" * 16 + "-01", "ff-" + "a" * 32 + "-" + "b" * 16 + "-01", "00-a-b-01", 5):
            self.assertIsNone(rt.parse_traceparent(bad), bad)
        with rt.trace() as t:
            self.assertEqual(rt.parse_traceparent(rt.traceparent())[0], t)
            seen = []
            pol = Policy("d")
            observed("m.f", lambda: pol.call(lambda a: seen.append(a.headers["traceparent"])))()
            self.assertEqual(rt.parse_traceparent(seen[0])[0], t)
            self.assertNotEqual(rt.parse_traceparent(seen[0])[1], rt.span_id())
        p = subprocess.run([os.sys.executable, "-c", "from ras_anything import runtime as r; print(r.trace_id())"], capture_output=True, text=True,
                           env=dict(os.environ, TRACEPARENT="00-" + "c" * 32 + "-" + "d" * 16 + "-00"))
        self.assertEqual(p.stdout.strip(), "c" * 32)

    def test_every_call_has_its_own_trace_unless_there_is_one(self):
        ids = []
        rt._trace.set(None)
        f = observed("m.f", lambda: ids.append(rt.trace_id()))
        f(), f()
        self.assertEqual(len(set(ids)), 2)


class SwapTest(Case):
    def test_async_functions_stay_async_and_observed(self):
        async def orig(x):
            return x

        async def hard(x):
            async def attempt():
                return Failing(*errors)()
            return await rt.aretry(attempt, attempts=1)
        errors = []
        f = swap("m.f", orig, hard, DEP)
        self.assertTrue(asyncio.iscoroutinefunction(f))
        self.assertEqual(asyncio.run(f(1)), "ok")
        errors.append(TimeoutError())
        with self.assertRaises(TimeoutError):
            asyncio.run(f(1))
        self.assertEqual([i["dependency"] for i in self.incidents()], ["charges"])


class FaultClassTest(unittest.TestCase):
    def test_classes(self):
        for e, c in ((TimeoutError(), "timeout"), (urllib.error.URLError(TimeoutError("timed out")), "timeout"), (subprocess.TimeoutExpired("x", 1), "timeout"),
                     (urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), "refused"), (BlockingIOError(11, "x"), "refused"),
                     (http_error(503), "unavailable"), (http_error(429), "rate_limited"), (http_error(401), "auth"), (http_error(404), "invalid_request"),
                     (http_error(409), "conflict"), (http_error(402), "quota"), (http_error(501), "unsupported"), (http_error(504), "timeout"),
                     (json.JSONDecodeError("Unterminated string", "", 0), "truncated"), (ConnectionResetError(), "unavailable"),
                     (Exception("renderer exited 75: "), "unavailable"), (KeyError("x"), "bug"), (TypeError("x"), "bug"), (Exception("?"), "unknown"),
                     (RasError("gone", cause=http_error(503)), "unavailable"), (CircuitOpenError("open"), "circuit_open"), (ax.ApprovalRequired("charge"), "approval")):
            self.assertEqual(rt.fault_class(e), c, repr(e))


class MaskTest(unittest.TestCase):
    def test_mask_and_plain(self):
        for text, gone in (("mail bob.smith@corp.example now", "bob"), ("card 4111 1111 1111 1111.", "1111"), ("call +1 415 555 2671", "555"),
                           ("Authorization: Bearer abcdef0123456789", "abcdef"), ("api_key=sk-live-aaaaaaaaaaaaaaaa", "aaaa"), ("https://u:pw@h/x", "pw"),
                           ("token=abc123", "abc123"), ('{"password": "hunter2"}', "hunter2")):
            self.assertNotIn(gone, rt.mask(text), text)
        for text in ("order 1234567 for 1500 cents", "trace " + "a1" * 16, "2026-11-23T15:35:00", "key 3f2b-11aa-8c0d", "http://127.0.0.1:43121/v1", "4111 1111 1111 1112"):
            self.assertEqual(rt.mask(text), text)
        for text, gone in (("GET http://10.0.0.1:81/a?b=1 failed", "10.0"), ("at 10.0.0.1:81", "10.0"), ("db.internal:5432 is down", "internal"),
                           ('File "/home/x/app.py", line 3, in f', "app.py"), ("open /etc/app/secret.yml", "etc"), ("localhost:80", "localhost"), ("::1 refused", "::1"),
                           ("boom\n    at f (/srv/app.mjs:3:1)", "srv")):
            self.assertNotIn(gone, rt.plain(text), text)


class ProblemTest(Case):
    def test_shape_and_advice(self):
        with rt.trace() as t:
            e = RasError("charging bob@x.com via http://10.0.0.1/v1 failed", cause=http_error(429, retry_after=7), dependency="payments")
        p = json.loads(e.to_json())
        self.assertEqual(p, {"type": "urn:ras-anything:problem:rate_limited", "title": ax.CAUSES["rate_limited"][0], "status": 429,
                             "detail": "charging [email] via [url] failed", "instance": "urn:trace:" + t, "cause": "rate_limited", "dependency": "payments",
                             "retryable": True, "retry_after_s": 7.0, "next_action": "retry_later", "needs_human_authorization": False, "correlation_id": t})
        self.assertEqual(str(e), "charging [email] via [url] failed (cause: rate_limited; next: retry_later; trace_id=%s)" % t)

    def test_every_cause(self):
        for e, cause, action in ((TimeoutError(), "timeout", "retry_later"), (http_error(503), "unavailable", "retry_later"), (http_error(402), "quota", "escalate"),
                                 (http_error(422), "invalid_request", "change_params"), (http_error(403), "auth", "escalate"), (http_error(409), "conflict", "stop"),
                                 (http_error(501), "dependency_bug", "escalate"), (KeyError("bug"), "unknown", "stop"), (CircuitOpenError("x"), "unavailable", "retry_later")):
            p = RasError("no", cause=e).problem
            self.assertEqual((p["cause"], p["next_action"]), (cause, action), repr(e))
            self.assertEqual(p["retryable"], action == "retry_later")
        self.assertEqual(RasError("x", cause=http_error(422)).problem["status"], 422)
        p = RasError("x", next_action="use_fallback", retryable=False, category="timeout").problem
        self.assertEqual((p["next_action"], p["retryable"]), ("use_fallback", False))

    def test_breaker_says_when(self):
        b = CircuitBreaker(fail_threshold=1, cooldown_s=30, name="shop")
        with self.assertRaises(TimeoutError):
            b.call(Failing(TimeoutError()))
        with self.assertRaises(CircuitOpenError) as cm:
            b.call(Failing())
        p = cm.exception.problem
        self.assertEqual((p["dependency"], p["cause"]), ("shop", "unavailable"))
        self.assertTrue(29 < p["retry_after_s"] <= 30)

    def test_approval_gate(self):
        with self.assertRaises(ax.ApprovalRequired) as cm:
            rt.require_approval("charge", {"amount_cents": 900, "email": "a@b.c"})
        p = cm.exception.problem
        self.assertEqual((p["needs_human_authorization"], p["next_action"], p["retryable"], p["status"]), (True, "escalate", False, 403))
        self.assertNotIn("a@b.c", json.dumps(p) + str(cm.exception))
        with mock.patch.dict(os.environ, {"RAS_ANYTHING_APPROVE": "refund, charge"}):
            self.assertTrue(rt.require_approval("charge"))
            self.assertRaises(ax.ApprovalRequired, rt.require_approval, "delete")
        rules = Path(self.tmp.name) / "approvals.json"
        with mock.patch.dict(os.environ, {"RAS_ANYTHING_APPROVALS": str(rules)}):
            rules.write_text(json.dumps({"send": True, "charge": {"max": {"amount_cents": 5000}}}))
            self.assertTrue(rt.require_approval("send"))
            self.assertTrue(rt.require_approval("charge", {"amount_cents": 5000}))
            for d in ({"amount_cents": 5001}, {}, {"amount_cents": "1"}, {"amount_cents": True}):
                self.assertRaises(ax.ApprovalRequired, rt.require_approval, "charge", d)
            rules.write_text("{broken")
            self.assertRaises(ax.ApprovalRequired, rt.require_approval, "send")
        for fn, ok in ((lambda op, d: True, True), (lambda op, d: "yes", False), (lambda op, d: 1 / 0, False)):
            prev = rt.set_approver(fn)
            try:
                if ok:
                    self.assertTrue(rt.require_approval("x"))
                else:
                    self.assertRaises(ax.ApprovalRequired, rt.require_approval, "x")
            finally:
                rt.set_approver(prev)


class HealthTest(Case):
    def test_breaker_and_functions(self):
        path = Path(self.tmp.name) / "health.json"
        with mock.patch.dict(os.environ, {"RAS_ANYTHING_HEALTH_FILE": str(path)}):
            b = CircuitBreaker(fail_threshold=1, cooldown_s=0.05, name="shop")
            f = observed("m.f", lambda *e: b.call(Failing(*e)), DEP)
            self.assertEqual(f(), "ok")
            self.assertEqual((rt.health()["status"], rt.health()["ready"]), ("ok", True))
            self.assertRaises(ConnectionRefusedError, f, ConnectionRefusedError())
            h = json.loads(path.read_text())  # written at the transition
            self.assertEqual((h["status"], h["ready"], h["live"]), ("degraded", False, True))
            self.assertIn(("shop", "open"), [(x["name"], x["state"]) for x in h["breakers"]])
            time.sleep(0.06)
            self.assertEqual(f(), "ok")
            h = rt.health()
            self.assertTrue(h["ready"])
            self.assertEqual((h["functions"]["m.f"]["calls"], h["functions"]["m.f"]["raised"]), (3, 1))
        states = [(r["Attributes"]["ras.breaker.previous_state"], r["Attributes"]["ras.breaker.state"]) for r in self.records("ras.breaker.transition")]
        self.assertEqual(states, [("closed", "open"), ("open", "half_open"), ("half_open", "closed")])


class RecorderTest(Case):
    def test_incident_document(self):
        f = observed("billing.charge", charge, DEP)
        f({"id": "INV-1"})
        with self.assertRaises(TimeoutError):
            f({"id": "INV-2", "email": "bob@x.com", "note": "card 4111111111111111", "password": "hunter2", "cents": 1500}, fail=[TimeoutError("timed out")] * 3)
        (i,) = self.incidents()
        self.assertEqual((i["kind"], i["function"], i["trigger"], i["fault"], i["dependency"]), ("incident", "billing.charge", "raised", "timeout", "charges"))
        self.assertEqual([e["event"] for e in i["events"]], ["failure", "retry", "failure", "retry", "failure", "gave_up"])
        self.assertEqual(len(i["before"]), 1)
        text = json.dumps(i)
        for secret in ("bob@x.com", "4111111111111111", "hunter2"):
            self.assertNotIn(secret, text)
        self.assertEqual(i["inputs"]["order"]["cents"], 1500)
        self.assertRegex(i["id"], r"^inc-\d{8}T\d{6}-billing\.charge-[0-9a-f]{8}$")
        self.assertIn(i["trace_id"], {r["TraceId"] for r in self.records()})

    def test_same_input_same_hash(self):
        def g(a, b=2, *, c=None):
            pass
        h = recorder.input_hash
        self.assertEqual(h(g, (1,), {}), h(g, (), {"b": 2, "a": 1}))
        self.assertEqual(h(g, ({"x": 1, "y": (1, 2)},), {}), h(g, ({"y": [1, 2], "x": 1},), {}))
        self.assertNotEqual(h(g, (1,), {}), h(g, (2,), {}))

    def test_bounded(self):
        f = observed("m.f", charge, DEP)
        for _ in range(9):
            with self.assertRaises(TimeoutError):
                f(1, fail=[TimeoutError()] * 3)
        self.assertEqual(len(self.incidents()), recorder.PER_KIND)
        for _ in range(200):
            f(1)
        self.assertEqual(len(recorder._ring), recorder.RING)
        big = recorder.redact({"list": list(range(1000)), "text": "x" * 5000, "deep": {"a": {"b": {"c": {"d": {"e": 1}}}}}})
        self.assertLess(len(json.dumps(big)), 1500)

    def test_off_and_give_up_without_an_exception(self):
        def degrading():
            try:
                return retry(Failing(*[TimeoutError()] * 3), attempts=2, base=0)
            except TimeoutError:
                return None
        with mock.patch.dict(os.environ, {"RAS_ANYTHING_INCIDENTS": "off"}):
            self.assertIsNone(observed("m.f", degrading, DEP)())
        self.assertEqual(self.incidents(), [])
        observed("m.f", degrading, DEP)()
        self.assertEqual([i["trigger"] for i in self.incidents()], ["gave_up"])

    def test_names(self):
        for n in ("password", "customerEmail", "api_key", "cardNumber", "Authorization", "first_name"):
            self.assertTrue(recorder.sensitive(n), n)
        for n in ("idempotency_key", "filename", "amount_cents", "sku", "id", "country"):
            self.assertFalse(recorder.sensitive(n), n)
        with mock.patch.dict(os.environ, {"RAS_ANYTHING_REDACT": "sku"}):
            self.assertTrue(recorder.sensitive("sku"))


HEAD = """
import { retry, retrySync, Policy, RasError, HttpError, CircuitBreaker, observe, problem, requireApproval, setApprover, withTrace, traceparent, health,
  faultClass } from %s;
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const core = require(%s);
const recorder = require(%s);
const err = (code) => Object.assign(new Error(code), { code });
const caught = (p) => Promise.resolve().then(() => p()).then(() => null, (e) => e);
const DEP = %s;
const done = (x) => console.log(JSON.stringify(x));
const failing = (...es) => async () => { if (es.length) throw es.shift(); return "ok"; };
""" % (json.dumps("file://" + str(AGENT / "runtime.cjs")), json.dumps(str(AGENT / "patterns/core.cjs")), json.dumps(str(AGENT / "patterns/recorder.cjs")),
       json.dumps(DEP))


@needs_node
class NodeTest(unittest.TestCase):
    def probe(self, code, env=None):
        with mock.patch.dict(os.environ, {"RAS_ANYTHING_INCIDENTS": "off", **(env or {})}):
            p = run_js(HEAD + code)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout), [json.loads(ln) for ln in p.stderr.splitlines()]

    def test_records(self):
        r, logs = self.probe("""
            const v = await withTrace(() => observe("orders.charge", DEP, () => retry(failing(new HttpError(503, { url: "http://10.1.2.3:8080/v1/charges" }),
              err("ECONNREFUSED")), { baseMs: 0 }), null, []), "00-" + "a".repeat(32) + "-" + "b".repeat(16) + "-01");
            const pol = new Policy("shop", { retry: { attempts: 2, baseMs: 0 }, circuitBreaker: { failThreshold: 2 }, fallback: { degraded: 7 } });
            const w = await observe("m.f", DEP, () => pol.call(failing(...Array(9).fill(0).map(() => err("ECONNREFUSED"))), { method: "POST", idempotencyKey: "k" }), null, []);
            const h = health();
            done([v, w, h.status, h.ready, h.functions["m.f"].gave_up, traceparent().length]);""")
        self.assertEqual(r, ["ok", 7, "degraded", False, 1, 55])
        self.assertEqual([x["EventName"] for x in logs], ["ras.dependency.failure", "ras.retry", "ras.dependency.failure", "ras.retry", "ras.dependency.recovered",
                                                          "ras.dependency.failure", "ras.retry", "ras.dependency.failure", "ras.breaker.transition",
                                                          "ras.dependency.gave_up", "fallback"])
        self.assertTrue(all(x["TraceId"] == "a" * 32 for x in logs[:5]))
        a = logs[0]["Attributes"]
        self.assertEqual((a["ras.dependency"], a["server.address"], a["server.port"], a["error.type"], a["ras.fault"], a["code.function"]),
                         ("charges", "10.1.2.3", 8080, "503", "unavailable", "orders.charge"))
        self.assertEqual((logs[2]["Attributes"]["ras.fault"], logs[2]["Attributes"]["url.template"]), ("refused", "/v1/charges"))
        up = logs[9]["Attributes"]
        self.assertEqual((logs[9]["SeverityText"], up["ras.next_action"], up["ras.idempotency_key"]), ("ERROR", "retry_later", "k"))

    def test_problems_are_pythons(self):
        r, _ = self.probe("""
            const P = require(%s);
            const es = [429, 503, 402, 422, 403, 409, 501, 504, 404].map((s) => new HttpError(s, { headers: { "retry-after": "7" } }));
            es.push(err("ECONNREFUSED"), err("ETIMEDOUT"), new SyntaxError("x"), new TypeError("bug"));
            done([P.CAUSES, P.NEXT_ACTIONS, es.map((e) => { const p = new RasError("to bob@x.com via http://10.0.0.1/x", { cause: e, dependency: "pay", traceId: "t" }).problem; return p; }),
              String(new RasError("x")), Object.keys(new RasError("x"))]);""" % json.dumps(str(AGENT / "patterns/problem.cjs")))
        self.assertEqual(r[0], {k: list(v) for k, v in ax.CAUSES.items()})
        self.assertEqual(r[1], list(ax.NEXT_ACTIONS))
        mine = [http_error(s, 7) for s in (429, 503, 402, 422, 403, 409, 501, 504, 404)] + [ConnectionRefusedError(), TimeoutError(), json.JSONDecodeError("x", "", 0), TypeError("bug")]
        for p, e in zip(r[2], mine):
            q = RasError("to bob@x.com via http://10.0.0.1/x", cause=e, dependency="pay", trace_id="t").problem
            self.assertEqual(p, json.loads(json.dumps(q)), repr(e))
        self.assertRegex(r[3], r"^RasError: x \(next: stop; trace_id=[0-9a-f]{32}\)$")
        self.assertNotIn("context", r[4])

    def test_masking_is_pythons(self):
        texts = ["mail bob.smith@corp.example now", "card 4111 1111 1111 1111.", "call +1 415 555 2671", "Authorization: Bearer abcdef0123456789",
                 "api_key=sk-live-aaaaaaaaaaaaaaaa", "https://u:pw@h/x", '{"password": "hunter2"}', "order 1234567 for 1500 cents", "GET http://10.0.0.1:81/a?b=1 failed",
                 "db.internal:5432 is down", 'File "/home/x/app.py", line 3, in f', "open /etc/app/secret.yml", "localhost:80 and ::1", "fine, really: 12.5% of 3/4"]
        r, _ = self.probe("done(%s.map((t) => [core.mask(t), core.plain(t)]));" % json.dumps(texts))
        self.assertEqual(r, [[rt.mask(t), rt.plain(t)] for t in texts])

    def test_approval(self):
        r, _ = self.probe("""
            const e = await caught(() => requireApproval("charge", { amount_cents: 1 }));
            setApprover(async (op) => op === "charge");
            const ok = await requireApproval("charge");
            const no = await caught(() => requireApproval("delete"));
            setApprover(() => { throw new Error("x"); });
            const failed = await caught(() => requireApproval("charge"));
            done([e.problem.needs_human_authorization, e.problem.next_action, e.problem.status, ok, no.name, failed.name]);""")
        self.assertEqual(r, [True, "escalate", 403, True, "ApprovalRequired", "ApprovalRequired"])
        r, _ = self.probe('done([await requireApproval("charge"), (await caught(() => requireApproval("x"))).name]);', {"RAS_ANYTHING_APPROVE": "charge"})
        self.assertEqual(r, [True, "ApprovalRequired"])

    def test_incident(self):
        with tempfile.TemporaryDirectory() as d:
            r, logs = self.probe("""
                const f = (order) => observe("orders.charge", DEP, () => retry(failing(...Array(3).fill(0).map(() => err("ECONNREFUSED"))), { baseMs: 0 }), null, [order]);
                await caught(() => f({ id: 2, email: "bob@x.com", note: "card 4111111111111111", cents: 1500 }));
                for (let i = 0; i < 9; i++) await caught(() => f(1));
                const sync = await caught(() => observe("m.g", [], () => { throw new TypeError("bug"); }, null, []));
                done([sync.name]);""", {"RAS_ANYTHING_INCIDENTS": d})
            docs = [json.loads(p.read_text()) for p in sorted(Path(d).glob("inc-*.json"))]
        ours = [i for i in docs if i["function"] == "orders.charge"]
        self.assertEqual(len(ours), recorder.PER_KIND)
        i = next(i for i in ours if i["inputs"] != [1])
        self.assertEqual((i["trigger"], i["fault"], i["dependency"], i["inputs"][0]["email"], i["inputs"][0]["cents"]), ("raised", "refused", "charges", "[redacted]", 1500))
        self.assertEqual([e["event"] for e in i["events"]], ["failure", "retry", "failure", "retry", "failure", "gave_up"])
        self.assertNotIn("4111111111111111", json.dumps(docs))
        self.assertEqual([i["fault"] for i in docs if i["function"] == "m.g"], ["bug"])
        self.assertEqual(set(i), {"schema_version", "kind", "id", "created", "lang", "service", "pid", "function", "trigger", "trace_id", "span_id", "parent_span_id", "seed",
                                  "started", "duration_s", "inputs", "input_hash", "dependencies", "dependency", "fault", "outcome", "events", "events_dropped",
                                  "guards_at_start", "before"})


if __name__ == "__main__":
    unittest.main()
