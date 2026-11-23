"""The Node runtime helpers (lang/node_agent/runtime.cjs), from CommonJS and from ES modules, on real time."""

import json
import unittest

from tests.node_support import AGENT, NODE, needs_node, run_js, run_json

HEAD = """
import { retry, retrySync, withTimeout, withFallback, isTransient, raiseForStatus, retryAfterS, RasError, HttpError, CircuitOpenError, CircuitBreaker,
  logEvent, traceId, withTrace, seed, sleepSync, scope } from %s;
const done = (x) => console.log(JSON.stringify(x));
const err = (code) => Object.assign(new Error(code), { code });
const caught = (p) => Promise.resolve(p).then(() => null, (e) => e);
""" % json.dumps("file://" + str(AGENT / "runtime.cjs"))


def probe(code):
    return run_json(HEAD + code)


@needs_node
class RetryTest(unittest.TestCase):
    def test_retries_transient_errors_only_and_rethrows_the_last_unchanged(self):
        r = probe("""
            let n = 0, m = 0, k = 0;
            const v = await retry(() => { if (++n < 3) throw err("ECONNRESET"); return "ok"; }, { baseMs: 1 });
            const last = err("ECONNREFUSED");
            const same = (await caught(retry(() => { m++; throw last; }, { attempts: 4, baseMs: 1 }))) === last;
            const bug = await caught(retry(() => { k++; null.x; }, { baseMs: 1 }));
            done([v, n, m, same, k, bug.name]);""")
        self.assertEqual(r, ["ok", 3, 4, True, 1, "TypeError"])

    def test_budget_and_retry_after(self):
        r = probe("""
            const http = (status, ra) => new HttpError(status, { headers: ra ? { "retry-after": ra } : {} });
            let n = 0;
            const t0 = Date.now();
            await caught(retry(() => { n++; throw http(429, "10"); }, { budgetMs: 500 }));
            const quick = Date.now() - t0 < 400;
            const t1 = Date.now();
            let k = 0;
            await retry(() => { if (k++ === 0) throw http(503, "0.2"); }, { baseMs: 1 });
            const waited = Date.now() - t1;
            let m = 0;
            await caught(retry(() => { m++; throw http(404); }));
            done([n, quick, waited >= 190, m, retryAfterS(http(429, new Date(Date.now() + 60000).toUTCString())) > 50, retryAfterS(http(429))]);""")
        self.assertEqual(r, [1, True, True, 1, True, None])

    def test_each_attempt_is_bounded_even_if_it_ignores_the_signal(self):
        r = probe("""
            const t0 = Date.now();
            const aborted = [];
            const e = await caught(retry(({ signal, attempt }) => { signal.addEventListener("abort", () => aborted.push(attempt)); return new Promise(() => {}); },
              { attempts: 2, timeoutMs: 60, baseMs: 1 }));
            const f = await caught(retry(() => new Promise(() => {}), { attempts: 9, budgetMs: 150 }));
            done([e.name, aborted, f.name, Date.now() - t0 < 1500]);""")
        self.assertEqual(r, ["TimeoutError", [1, 2], "TimeoutError", True])

    def test_sync(self):
        r = probe("""
            let n = 0, seen = [];
            const v = retrySync(({ timeoutMs, attempt }) => { seen.push([attempt, timeoutMs <= 500]); if (++n < 3) throw Object.assign(new Error("x"), { status: 75 }); return n; },
              { timeoutMs: 500, budgetMs: 2000, baseMs: 1 });
            const t0 = Date.now(); sleepSync(50);
            done([v, seen, Date.now() - t0 >= 45]);""")
        self.assertEqual(r, [3, [[1, True], [2, True], [3, True]], True])

    def test_with_timeout(self):
        r = probe("""
            const a = await withTimeout(async () => "fast", 1000);
            const b = await caught(withTimeout((signal) => new Promise((_, rej) => signal.addEventListener("abort", () => rej(new Error("late")))), 30));
            const c = new AbortController();
            setTimeout(() => c.abort(new Error("outer")), 20);
            const d = await caught(withTimeout(() => new Promise(() => {}), 5000, { signal: c.signal }));
            done([a, b.name, d.message]);""")
        self.assertEqual(r, ["fast", "TimeoutError", "outer"])

    def test_jitter_is_seedable(self):
        r = probe("""
            const draws = async () => { seed(7); const d = []; await caught(retry(() => { throw err("EPIPE"); }, { attempts: 4, baseMs: 2, onRetry: (n, e, ms) => d.push(ms) })); return d; };
            const [a, b] = [await draws(), await draws()];
            done([a.length, JSON.stringify(a) === JSON.stringify(b), a.every((x, i) => x >= 0 && x <= 2 * 2 ** i)]);""")
        self.assertEqual(r, [3, True, True])


@needs_node
class ErrorsTest(unittest.TestCase):
    def test_is_transient(self):
        r = probe("""
            const wrapped = new TypeError("fetch failed", { cause: err("ECONNREFUSED") });
            const yes = [err("ETIMEDOUT"), err("EAI_AGAIN"), err("EAGAIN"), wrapped, new DOMException("x", "TimeoutError"), new HttpError(503), new HttpError(429), new HttpError(408),
              Object.assign(new Error("Command failed"), { code: 75 }), Object.assign(new Error("k"), { killed: true, signal: "SIGTERM" }), new TypeError("terminated"),
              new RasError("x", { cause: err("ECONNRESET") })];
            const no = [new HttpError(404), new HttpError(400), new HttpError(501), new TypeError("x is not a function"), new SyntaxError("bad"), err("ENOENT"),
              Object.assign(new Error("Command failed"), { code: 1 }), null, "text", new DOMException("x", "AbortError")];
            done([yes.map(isTransient), no.map(isTransient)]);""")
        self.assertEqual(set(r[0]), {True}, r[0])
        self.assertEqual(set(r[1]), {False}, r[1])

    def test_raise_for_status(self):
        r = probe("""
            const ok = new Response("x");
            const same = (await raiseForStatus(ok)) === ok;
            const e = await caught(raiseForStatus(new Response("busy", { status: 429, headers: { "Retry-After": "3" } })));
            done([same, e.name, e.status, e.body, retryAfterS(e)]);""")
        self.assertEqual(r, [True, "HttpError", 429, "busy", 3])

    def test_ras_error_and_log_lines_share_a_trace_id(self):
        out = run_js(HEAD + """
            const e = withTrace(() => { logEvent("gave_up", { order: 7, error: err("EPIPE"), deep: { a: 1 } }); return new RasError("charge failed", { cause: err("EPIPE"), context: { order: 7 } }); });
            done([e instanceof Error, e.message, String(e), e.cause.code, JSON.parse(JSON.stringify(e)), e.traceId]);""")
        line = json.loads(out.stderr)
        r = json.loads(out.stdout)
        self.assertEqual((line["event"], line["order"], line["error"], line["deep"]), ("gave_up", 7, "Error: EPIPE", '{"a":1}'))
        self.assertEqual(line["trace_id"], r[5])
        self.assertRegex(r[5], r"^[0-9a-f]{16}$")
        self.assertEqual(r[:2] + r[3:4], [True, "charge failed", "EPIPE"])
        self.assertIn("charge failed | order=7 | cause=Error: EPIPE | trace_id=" + r[5], r[2])
        self.assertEqual(r[4]["trace_id"], r[5])

    def test_fallback_is_logged(self):
        out = run_js(HEAD + """
            const a = await withFallback(async () => { throw err("ECONNRESET"); }, (e) => e.code, { event: "degraded", fields: { id: 1 } });
            const b = await caught(withFallback(() => { throw new TypeError("bug"); }, "never"));
            done([a, b.name]);""")
        self.assertEqual(json.loads(out.stdout), ["ECONNRESET", "TypeError"])
        self.assertEqual(json.loads(out.stderr)["event"], "degraded")


@needs_node
class BreakerTest(unittest.TestCase):
    def test_opens_half_opens_and_closes(self):
        r = probe("""
            const b = new CircuitBreaker({ failThreshold: 2, cooldownMs: 60 });
            const bad = () => { throw err("ECONNREFUSED"); };
            const out = [];
            out.push((await caught(b.call(bad))).code, b.state, (await caught(b.call(bad))).code, b.state);
            const open = await caught(b.call(() => "never"));
            out.push(open instanceof CircuitOpenError, open.name);
            await new Promise((r) => setTimeout(r, 80));
            out.push(b.state);
            const slow = b.call(() => new Promise((r) => setTimeout(() => r("probe"), 30)));
            out.push((await caught(b.call(() => "second"))) instanceof CircuitOpenError, await slow, b.state);
            await caught(b.call(() => { throw new TypeError("bug"); }));
            out.push(b.failures);
            done(out);""")
        self.assertEqual(r, ["ECONNREFUSED", "closed", "ECONNREFUSED", "open", True, "CircuitOpenError", "half_open", True, "probe", "closed", 0])


@needs_node
class ModuleTest(unittest.TestCase):
    def test_loads_from_commonjs_and_esm_and_needs_nothing_else(self):
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            shutil.copy(AGENT / "runtime.cjs", Path(d) / "_runtime.cjs")
            shutil.copytree(AGENT / "patterns", Path(d) / "patterns")
            (Path(d) / "a.cjs").write_text('const { retry } = require("./_runtime.cjs"); console.log(typeof retry);')
            (Path(d) / "b.mjs").write_text('import { retry, RasError } from "./_runtime.cjs"; console.log(typeof retry, typeof RasError);')
            for f, want in (("a.cjs", "function\n"), ("b.mjs", "function function\n")):
                p = subprocess.run([NODE, f], cwd=d, capture_output=True, text=True, timeout=30)
                self.assertEqual((p.stdout, p.stderr), (want, ""))

    def test_scope_names_only(self):
        r = probe("""done([(await caught((async () => scope("nope"))())).name]);""")
        self.assertEqual(r, ["RasError"])


if __name__ == "__main__":
    unittest.main()
