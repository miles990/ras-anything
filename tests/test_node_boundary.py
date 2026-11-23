"""Every Node boundary, intercepted and faulted live against a local server; injected failures must look like
the API's own. And virtual time."""

import json
import unittest

from tests.node_support import AGENT, needs_node, run_js, run_json

HEAD = """
import http from "node:http";
import * as https from "node:https";
import cp, { exec, execFile, spawn, execSync, execFileSync, spawnSync } from "node:child_process";
import { promisify, inspect } from "node:util";
import { createRequire } from "node:module";
import { start, seen } from "./upstream.mjs";
const require = createRequire(import.meta.url);
const B = require(%(agent)r + "/boundary.cjs"), F = require(%(agent)r + "/faults.cjs");
const stop = await start();
const API = process.env.API;
let hung = null;
const clock = new F.LiveClock(0.02, 0.3, (w) => { hung = w ?? true; console.log(JSON.stringify({ hang: true })); process.exit(0); });
F.patchAbortSignal((ms, fire) => setTimeout(fire, ms).unref());
const handler = new B.Faulting({ type: "none" }, clock);
B.install(handler);
const api = {
  fetchGet: async (o) => { const r = await fetch(API + "/a?x=1", o); return [r.status, r.headers.get("x-test"), r.headers.get("retry-after"), r.url.endsWith("/a?x=1"), await r.text()]; },
  fetchPost: async (o) => { const r = await fetch(API + "/p", { method: "POST", body: "hi", headers: { "Idempotency-Key": "k1" }, ...o }); return [r.status, await r.text()]; },
  httpGet: (o) => new Promise((res, rej) => {
    const q = http.get(API + "/h", o ?? {}, (r) => { let s = ""; r.setEncoding("utf8"); r.on("data", (c) => (s += c)); r.on("end", () => res([r.statusCode, r.headers["x-test"] ?? null, r.headers["retry-after"] ?? null, r.complete, s])); });
    q.on("error", rej); q.on("timeout", () => q.destroy(new Error("timed out")));
  }),
  httpPost: (o) => new Promise((res, rej) => {
    const u = new URL(API);
    const q = http.request({ hostname: u.hostname, port: u.port, path: "/hp", method: "POST", headers: { "Content-Type": "text/plain" }, ...o }, (r) => { const c = []; r.on("data", (x) => c.push(x)); r.on("end", () => res([r.statusCode, Buffer.concat(c).toString()])); });
    q.on("error", rej); q.on("timeout", () => q.destroy()); q.write("a"); q.end("b");
  }),
  exec: (o) => promisify(exec)("printf hello", o).then((r) => [r.stdout, r.stderr]),
  execFile: (o) => promisify(execFile)("printf", ["hello"], o).then((r) => [r.stdout, r.stderr]),
  execFileCb: (o) => new Promise((res, rej) => execFile("printf", ["hello"], o ?? {}, (e, so) => (e ? rej(e) : res([so])))),
  spawn: (o) => new Promise((res, rej) => { const c = spawn("printf", ["hello"], o ?? {}); let s = ""; c.stdout.on("data", (d) => (s += d)); c.on("error", rej); c.on("close", (code, sig) => res([code, sig, s])); }),
  execSync: (o) => [execSync("printf hello", { encoding: "utf8", ...o })],
  execFileSync: (o) => [execFileSync("printf", ["hello"], { encoding: "utf8", ...o })],
  spawnSync: (o) => { const r = spawnSync("printf", ["hello"], { encoding: "utf8", ...o }); return [r.status, r.signal, r.stdout, r.error?.code ?? null]; },
};
const shape = (e) => ({ name: e.name, message: e.message, code: e.code ?? null, errno: e.errno ?? null, syscall: e.syscall ?? null, killed: e.killed ?? null,
  signal: e.signal ?? null, status: e.status ?? null, cause: e.cause ? { name: e.cause.name, code: e.cause.code ?? null, syscall: e.cause.syscall ?? null, errno: e.cause.errno ?? null } : null });
async function call(name, fault, opts) {
  handler.reset();
  handler.plan = F.plan(fault ? { type: "first", fault, key: null } : { type: "none" });
  let out;
  try { out = { ok: await api[name](opts) }; } catch (e) { out = { err: shape(e) }; }
  await new Promise((r) => setTimeout(r, 5)); // (a live child's outcome is observed as it closes)
  return { ...out, attempts: handler.attempts.map(({ key, n, fault, reached, failed, idem }) => ({ key, n, fault, reached, failed, idem })) };
}
const done = (x) => { console.log(JSON.stringify(x)); stop(); };
""" % {"agent": str(AGENT)}

HTTP = ("fetchGet", "fetchPost", "httpGet", "httpPost")
CHILD = ("exec", "execFile", "execFileCb", "spawn", "execSync", "execFileSync", "spawnSync")


def probe(code: str):
    return run_json(HEAD + code)


@needs_node
class InterceptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = probe("const out = {}; for (const n of Object.keys(api)) out[n] = await call(n); done(out);")

    def test_every_boundary_is_seen_once_and_answers_as_usual(self):
        body = '{"path":"%s","method":"%s","sent":"%s"}'
        self.assertEqual(self.r["fetchGet"]["ok"], [200, "yes", None, True, body % ("/a?x=1", "GET", "")])
        self.assertEqual(self.r["fetchPost"]["ok"], [200, body % ("/p", "POST", "hi")])
        self.assertEqual(self.r["httpGet"]["ok"], [200, "yes", None, True, body % ("/h", "GET", "")])
        self.assertEqual(self.r["httpPost"]["ok"], [200, body % ("/hp", "POST", "ab")])
        for n in ("exec", "execFile"):
            self.assertEqual(self.r[n]["ok"], ["hello", ""])
        for n in ("execFileCb", "execSync", "execFileSync"):
            self.assertEqual(self.r[n]["ok"], ["hello"])
        self.assertEqual(self.r["spawn"]["ok"], [0, None, "hello"])
        self.assertEqual(self.r["spawnSync"]["ok"], [0, None, "hello", None])
        for n in HTTP + CHILD:
            (a,) = self.r[n]["attempts"]
            self.assertEqual((a["fault"], a["reached"], a["failed"]), (None, True, False), n)

    def test_keys_have_no_port_and_hash_the_body(self):
        import hashlib

        key = {n: self.r[n]["attempts"][0]["key"] for n in HTTP + CHILD}
        self.assertEqual(key["fetchGet"], "GET http://127.0.0.1/a?x=1")
        self.assertEqual(key["fetchPost"], "POST http://127.0.0.1/p #" + hashlib.sha256(b"hi").hexdigest()[:12])
        self.assertEqual(key["httpPost"], "POST http://127.0.0.1/hp #" + hashlib.sha256(b"ab").hexdigest()[:12])
        for n in CHILD:
            self.assertEqual(key[n], "run printf hello")
        self.assertEqual(self.r["fetchPost"]["attempts"][0]["idem"], "k1")

    def test_bodies_of_every_kind_have_a_stable_key(self):
        r = probe("""
            const keys = [];
            for (const body of ["x=1", new URLSearchParams("x=1"), Buffer.from("x=1"), new Uint8Array([120, 61, 49]), new Blob(["x=1"])]) {
              handler.reset();
              await (await fetch(API + "/b", { method: "POST", body })).text();
              keys.push(handler.attempts[0].key);
            }
            handler.reset();
            const req = new Request(API + "/b", { method: "PUT", body: "x=1" });
            const sent = await (await fetch(req)).json();
            done([keys, handler.attempts[0].key, sent.sent]);""")
        self.assertEqual(len(set(r[0])), 1)
        self.assertTrue(r[1].startswith("PUT http://127.0.0.1/b #"))
        self.assertEqual(r[2], "x=1")

    def test_named_esm_imports_and_commonjs_see_the_patch(self):
        r = probe("""
            handler.reset();
            const { get } = await import("node:http");
            const cjs = require("http"), ccp = require("child_process");
            const read = (f) => new Promise((ok) => f(API + "/n", (r) => { r.resume(); r.on("end", ok); }));
            await read(get); await read(cjs.get);
            ccp.execFileSync("printf", ["a"]);
            done(handler.attempts.map((a) => a.key));""")
        self.assertEqual(r, ["GET http://127.0.0.1/n", "GET http://127.0.0.1/n", "run printf a"])

    def test_interpreter_and_paths_in_commands_are_portable(self):
        r = probe("""
            handler.reset();
            execFileSync(process.execPath, ["-e", "0"]);
            execFileSync("printf", [process.cwd() + "/sub/x"]);
            done(handler.attempts.map((a) => a.key));""")
        self.assertEqual(r, ["run node -e 0", "run printf sub/x"])


@needs_node
class FaultTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = probe("""
            const out = {};
            const bounded = { fetchGet: () => ({ signal: AbortSignal.timeout(1000) }), fetchPost: () => ({ signal: AbortSignal.timeout(1000) }) };
            for (const n of Object.keys(api)) {
              out[n] = {};
              for (const f of ["refused", "http5xx", "http429", "truncated", "timeout", "slow"]) {
                seen.length = 0;
                out[n][f] = await call(n, f, f === "timeout" ? bounded[n]?.() ?? { timeout: 1000 } : undefined);
                out[n][f].seen = seen.length;
              }
            }
            done(out);""")

    def attempt(self, n, f):
        (a,) = self.r[n][f]["attempts"]
        self.assertEqual(a["fault"], f)
        return a

    def test_refused_looks_like_the_real_thing(self):
        real = probe("""
            B.install(new B.Watcher());
            const API2 = await new Promise((r) => { const s = http.createServer().listen(0, "127.0.0.1", () => { const u = `http://127.0.0.1:${s.address().port}`; s.close(() => r(u)); }); });
            const out = {};
            try { await fetch(API2); } catch (e) { out.fetch = shape(e); }
            out.http = await new Promise((r) => http.get(API2).on("error", (e) => r(shape(e))));
            done(out);""")
        for n in ("fetchGet", "fetchPost"):
            e = self.r[n]["refused"]["err"]
            self.assertEqual((e["name"], e["message"], e["cause"]), (real["fetch"]["name"], real["fetch"]["message"], real["fetch"]["cause"]))
        for n in ("httpGet", "httpPost"):
            e = self.r[n]["refused"]["err"]
            self.assertEqual({k: e[k] for k in ("name", "code", "errno", "syscall")}, {k: real["http"][k] for k in ("name", "code", "errno", "syscall")})
        for n in CHILD[:-1]:
            e = self.r[n]["refused"]["err"]
            self.assertEqual((e["code"], e["errno"]), ("EAGAIN", -11), n)
            self.assertTrue(e["syscall"].startswith("spawn"), n)
        self.assertEqual(self.r["spawnSync"]["refused"]["ok"], [None, None, None, "EAGAIN"])
        for n in HTTP + CHILD:
            a = self.attempt(n, "refused")
            self.assertEqual((a["reached"], a["failed"]), (False, True), n)
            self.assertEqual(self.r[n]["refused"]["seen"], 0)

    def test_5xx_and_429(self):
        for n in HTTP:
            self.assertEqual(self.r[n]["http5xx"]["ok"][0], 503)
            self.assertEqual(self.r[n]["http429"]["ok"][0], 429)
            self.assertTrue(self.attempt(n, "http5xx")["failed"])
        self.assertEqual(self.r["fetchGet"]["http429"]["ok"][2], "1")
        self.assertEqual(self.r["httpGet"]["http429"]["ok"][2], "1")
        for n in ("exec", "execFile", "execFileCb"):
            e = self.r[n]["http5xx"]["err"]
            self.assertEqual((e["code"], e["killed"], e["signal"]), (75, False, None))
            self.assertTrue(e["message"].startswith("Command failed: printf hello"))
        for n in ("execSync", "execFileSync"):
            self.assertEqual(self.r[n]["http5xx"]["err"]["status"], 75)
        self.assertEqual(self.r["spawn"]["http5xx"]["ok"], [75, None, ""])
        self.assertEqual(self.r["spawnSync"]["http5xx"]["ok"], [75, None, "", None])

    def test_truncated_bodies(self):
        for n in HTTP:
            text = self.r[n]["truncated"]["ok"][-1]
            self.assertTrue(text.startswith('{"path":'))
            self.assertFalse(text.endswith("}"))
            a = self.attempt(n, "truncated")
            self.assertEqual((a["reached"], a["failed"]), (True, True))
        for n in CHILD:
            out = self.r[n]["truncated"]["ok"]
            self.assertIn("he", out)
            self.assertTrue(self.attempt(n, "truncated")["failed"])

    def test_timeouts_fire_the_callers_own(self):
        for n in ("fetchGet", "fetchPost"):
            e = self.r[n]["timeout"]["err"]
            self.assertEqual((e["name"], e["message"]), ("TimeoutError", "The operation was aborted due to timeout"))
        self.assertEqual(self.r["httpGet"]["timeout"]["err"]["message"], "timed out")
        self.assertEqual(self.r["httpPost"]["timeout"]["err"]["code"], "ECONNRESET")
        for n in ("exec", "execFile", "execFileCb"):
            e = self.r[n]["timeout"]["err"]
            self.assertEqual((e["code"], e["killed"], e["signal"]), (None, True, "SIGTERM"))
        for n in ("execSync", "execFileSync"):
            self.assertEqual(self.r[n]["timeout"]["err"]["code"], "ETIMEDOUT")
        self.assertEqual(self.r["spawn"]["timeout"]["ok"], [None, "SIGTERM", ""])
        self.assertEqual(self.r["spawnSync"]["timeout"]["ok"], [None, "SIGTERM", "", "ETIMEDOUT"])
        for n in HTTP + CHILD:
            self.assertTrue(self.attempt(n, "timeout")["failed"], n)

    def test_a_timed_out_post_was_committed_and_only_its_answer_lost(self):
        for n in ("fetchPost", "httpPost"):
            self.assertTrue(self.attempt(n, "timeout")["reached"])
            self.assertEqual(self.r[n]["timeout"]["seen"], 1)
        for n in ("fetchGet", "httpGet"):
            self.assertFalse(self.attempt(n, "timeout")["reached"])
            self.assertEqual(self.r[n]["timeout"]["seen"], 0)

    def test_slow_is_late_but_right(self):
        for n in HTTP:
            self.assertEqual(self.r[n]["slow"]["ok"][0], 200)
            self.assertFalse(self.attempt(n, "slow")["failed"])
        for n in CHILD:
            self.assertIn("hello", self.r[n]["slow"]["ok"])

    def test_child_timeouts_match_real_ones(self):
        r = probe("""
            B.install(new B.Watcher());
            const real = await promisify(execFile)("sleep", ["5"], { timeout: 50 }).catch(shape);
            let sync; try { execFileSync("sleep", ["5"], { timeout: 50 }); } catch (e) { sync = shape(e); }
            B.install(handler);
            handler.plan = F.plan({ type: "first", fault: "timeout", key: null });
            const fake = await promisify(execFile)("sleep", ["5"], { timeout: 50 }).catch(shape);
            handler.reset();
            let fsync; try { execFileSync("sleep", ["5"], { timeout: 50 }); } catch (e) { fsync = shape(e); }
            done([real, fake, sync, fsync]);""")
        for k in ("name", "code", "killed", "signal"):
            self.assertEqual(r[0][k], r[1][k], k)
        for k in ("name", "code", "errno", "syscall", "signal", "status", "message"):
            self.assertEqual(r[2][k], r[3][k], k)


@needs_node
class HangTest(unittest.TestCase):
    def test_a_call_with_no_timeout_is_a_hang_whatever_catches_it(self):
        for name in ("fetchGet", "httpGet", "execFile", "spawn", "execFileSync", "spawnSync"):
            r = run_js(HEAD + "try { await call(%r, 'timeout'); } catch {} done({ hang: false });" % name)
            self.assertEqual(json.loads(r.stdout.splitlines()[0]), {"hang": True}, name + r.stderr)

    def test_a_callers_race_or_abort_controller_is_not_a_hang(self):
        r = probe("""
            handler.plan = F.plan({ type: "first", fault: "timeout", key: null });
            const out = [];
            out.push(await Promise.race([fetch(API + "/r"), new Promise((_, rej) => setTimeout(() => rej(new Error("mine")), 600))]).catch((e) => e.message));
            const c = new AbortController();
            setTimeout(() => c.abort(), 500);
            out.push(await fetch(API + "/c", { signal: c.signal }).catch((e) => e.name));
            out.push(await fetch(API + "/any", { signal: AbortSignal.any([new AbortController().signal, AbortSignal.timeout(60000)]) }).catch((e) => e.name));
            const child = spawn("sleep", ["9"]);
            setTimeout(() => child.kill("SIGKILL"), 300);
            out.push(await new Promise((r) => child.on("close", (c, s) => r(s))));
            done(out);
            process.exit(0); // (the call that lost the race is still open and would keep the process running, as in real life)""")
        self.assertEqual(r, ["mine", "AbortError", "TimeoutError", "SIGKILL"])

    def test_http_abort_signal_and_destroy(self):
        r = probe("""
            handler.plan = F.plan({ type: "first", fault: "timeout", key: null });
            const out = [];
            const get = (u, o, act) => new Promise((res) => { const q = http.get(u, o, () => res("answered")); q.on("error", (e) => res(e.code ?? e.name)); act?.(q); });
            out.push(await get(API + "/1", { signal: AbortSignal.timeout(30000) }));
            out.push(await get(API + "/2", {}, (q) => setTimeout(() => q.destroy(), 50)));
            out.push(await get(API + "/3", {}, (q) => q.setTimeout(30000, () => q.destroy(Object.assign(new Error("x"), { code: "MINE" })))));
            done(out);""")
        self.assertEqual(r, ["ABORT_ERR", "ECONNRESET", "MINE"])


@needs_node
class VirtualClockTest(unittest.TestCase):
    def test_timers_dates_and_signals_follow_it(self):
        r = run_json("""
            import { createRequire } from "node:module";
            import { setTimeout as sleep } from "node:timers/promises";
            import { promisify } from "node:util";
            const F = createRequire(import.meta.url)(%r);
            const real = setImmediate, t0 = process.hrtime.bigint();
            const clock = new F.VirtualClock();
            clock.install();
            let over = false;
            const out = [];
            const d0 = Date.now(), p0 = performance.now(), h0 = process.hrtime();
            (async () => {
              await new Promise((r) => setTimeout(r, 1500));
              out.push(Date.now() - d0);
              await sleep(500);
              out.push(new Date().getTime() - d0, Math.round(performance.now() - p0));
              await promisify(setTimeout)(1000);
              out.push(process.hrtime(h0)[0]);
              out.push(await sleep(99999, "x", { signal: AbortSignal.timeout(2000) }).catch((e) => e.name));
              out.push(Date.now() - d0);
              Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 1000);
              out.push(Date.now() - d0);
              let n = 0;
              await new Promise((r) => { const i = setInterval(() => { if (++n === 3) { clearInterval(i); r(); } }, 100); });
              out.push(Date.now() - d0, new Date(0) instanceof Date, Object.prototype.toString.call(new Date()), typeof Date());
              over = true;
            })();
            while (!over) { await new Promise((r) => real(r)); clock.advance(); }
            console.log(JSON.stringify(out));
            """ % str(AGENT / "faults.cjs"))
        self.assertEqual(r, [1500, 2000, 2000, 3, "AbortError", 5000, 6000, 6300, True, "[object Date]", "string"])


if __name__ == "__main__":
    unittest.main()
