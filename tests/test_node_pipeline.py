"""The whole loop on small Node apps: scan -> record -> verify (negative and positive controls) -> enable/disable
byte for byte, kill switch. Offline: the apps run their own local upstream."""

import hashlib
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from ras_anything import cassette
from ras_anything.bench import bench
from ras_anything.enable import disable, enable
from ras_anything.harden import harden
from ras_anything.paths import UserError
from ras_anything.scan import scan
from ras_anything.verify import verify
from tests.node_support import NODE, NodeCase, js, needs_node, needs_ts
from tests.test_portable import absolute_paths
from tests.test_pipeline import quiet


def run_app(env=None):
    return subprocess.run([NODE, "app.mjs"], capture_output=True, text=True, timeout=60, env={**os.environ, **(env or {})})


class Loop(NodeCase):
    """Helpers: scan the project in the current folder, harden one function, put code in, verify."""

    def scan(self, seeds=1):
        with quiet():
            self.s = scan(["node", "app.mjs"], seeds=seeds, quiet=True)
        self.fns = {f["id"]: f for f in self.s["functions"]}
        return self.s

    def harden(self, fid):
        with quiet():
            harden(fid, quiet=True)
        self.d = Path(".ras/hardens") / fid
        return self.d

    def hardened(self):
        return next(p for p in self.d.iterdir() if p.name.startswith("hardened."))

    def use(self, code):
        self.hardened().write_text(js(code))
        with quiet():
            return verify(self.d.name, quiet=True)

    def passes(self, v):
        self.assertTrue(v["passed"], json.dumps(v.get("fatal") or {"gates": v["gates"], "happy": v["happy"], "first": v["chaos"]["first_failure"]}, indent=1)[:3000])


GET = """
    export async function get(id) {
      const res = await fetch(`${process.env.API}/items/${id}`);
      return await res.json();
    }
    """
GET_GOOD = """
    import { isTransient, logEvent, raiseForStatus, RasError, retry } from "./_runtime.cjs";
    export async function get(id) {
      try {
        return await retry(async ({ signal }) => (await raiseForStatus(await fetch(`${process.env.API}/items/${id}`, { signal }))).json(),
          { attempts: 4, timeoutMs: 1500, budgetMs: 4700, retryOn: (e) => isTransient(e) || e instanceof SyntaxError });
      } catch (e) {
        logEvent("get.gave_up", { id, error: e });
        throw new RasError("get failed", { cause: e, context: { id } });
      }
    }
    """


@needs_node
class ScanTest(Loop):
    def test_attribution_is_async_aware_and_goes_to_the_innermost_exported_function(self):
        self.project({"lib.mjs": """
            async function rawFetch(p) { await null; return fetch(process.env.API + p); }
            export async function inner(id) { await new Promise((r) => setTimeout(r, 1)); return (await rawFetch("/i/" + id)).json(); }
            export async function outer(id) { const [a, b] = await Promise.all([inner(id), inner(id + 10)]); await (await rawFetch("/o")).text(); return [a.path, b.path]; }
            export class Client { async load(id) { return (await fetch(process.env.API + "/c/" + id)).text(); } }
            async function lonely() { return (await fetch(process.env.API + "/l")).text(); }
            export const viaClient = async (id) => new Client().load(id);
            export const all = async (id) => { const r = await outer(id); await viaClient(id); return r; };
            export const run = () => lonely();
            """, "extra.mjs": ""}, call="await lib.all(i)")
        self.scan()
        f = self.fns
        self.assertEqual({k: v["boundary"] for k, v in f.items() if v["boundary"]}, {"lib.inner": 6, "lib.outer": 3, "lib.viaClient": 3})
        self.assertEqual(f["lib.inner"]["boundary_kinds"], {"fetch": 6})
        self.assertEqual(f["lib.all"]["calls"], 3)
        self.assertIn("class method", f["lib.Client.load"]["verdict"])
        self.assertEqual(self.s["lang"], "node")
        self.assertEqual(absolute_paths(Path.cwd()), [])

    def test_what_is_not_exported_is_named_not_offered(self):
        self.project({"lib.mjs": """
            export const get = null;
            const load = async (id) => (await fetch(process.env.API + "/l/" + id)).text();
            globalThis.load = load;
            """}, call="(await globalThis.load(i)).length")
        self.scan()
        self.assertEqual(self.fns["lib.load"]["boundary"], 3)
        self.assertFalse(self.fns["lib.load"]["hardenable"])
        self.assertIn("not exported", self.fns["lib.load"]["verdict"])

    def test_values_that_are_not_data(self):
        self.project({"lib.mjs": """
            export async function get(id) { return fetch(process.env.API + "/" + id); }
            export async function withCallback(id, cb) { cb(await (await fetch(process.env.API + "/" + id)).text()); }
            """}, call="[(await lib.get(i)).status, await lib.withCallback(i, () => {})]")
        self.scan()
        self.assertIn("return value can't be stored", self.fns["lib.get"]["verdict"])
        self.assertIn("arguments can't be stored", self.fns["lib.withCallback"]["verdict"])

    def test_http_and_child_processes_are_counted(self):
        self.project({"lib.mjs": """
            import http from "node:http";
            import { exec, execFileSync } from "node:child_process";
            export function get(id) { return new Promise((res, rej) => http.get(process.env.API + "/" + id, (r) => { r.resume(); r.on("end", () => res(r.statusCode)); }).on("error", rej)); }
            export function sh() { return new Promise((res) => exec("printf x", (e, out) => res(out))); }
            export function sync() { return execFileSync("printf", ["x"], { encoding: "utf8" }); }
            """}, call="[await lib.get(i), await lib.sh(), lib.sync()]")
        self.scan()
        self.assertEqual(self.fns["lib.get"]["boundary_kinds"], {"http": 3})
        self.assertEqual(self.fns["lib.sh"]["boundary_kinds"], {"subprocess": 3})
        self.assertEqual(self.fns["lib.sync"]["boundary_kinds"], {"subprocess": 3})
        self.assertTrue(self.fns["lib.get"]["async"])  # (it returns a promise)
        self.assertFalse(self.fns["lib.sync"]["async"])

    def test_instrumenting_never_changes_the_app(self):
        """Every export style, printed with and without the agent."""
        self.project({"lib.cjs": """
            "use strict"
            const a = async (x) => x + 1
            function b(x) { return this === undefined ? "undef" : "this" }
            exports.c = (x) => [x].map((y) => y * 2)[0]
            module.exports = { a, b, c: exports.c, d(x) { return typeof this.a }, async e() { return (await fetch(process.env.API + "/e")).status } }
            class K { static s() { return 1 } m() { return super.toString().length } }
            module.exports.k = () => [K.s(), new K().m(), K.name, a.name, a.length, b.name]
            """}, "lib.cjs", "[await lib.a(i), lib.b(1), (0, lib.b)(1), lib.c(i), lib.d(), await lib.e(), lib.k()]")
        plain = run_app().stdout
        self.assertEqual(len(plain.splitlines()), 3)
        self.scan()
        self.assertEqual(Path(".ras/clean.stdout").read_text(), plain)
        self.assertEqual(self.fns["lib.e"]["boundary"], 3)

    def test_an_error_names_the_function_it_came_from(self):
        self.project({"lib.mjs": """
            import { execFile } from "node:child_process";
            import { promisify } from "node:util";
            export async function get() { return (await promisify(execFile)("printf", ["x"])).stdout; }
            """})
        self.scan(seeds=8)
        where = {(r["uncaught"] or {}).get("where") for r in self.s["chaos"]["runs"] if r["outcome"] in ("crash", "hang")}
        self.assertEqual(where, {"lib.get"})


@needs_node
class CassetteTest(Loop):
    def test_round_trip_of_values(self):
        self.project({"lib.mjs": """
            export async function get(id) {
              const status = (await fetch(`${process.env.API}/x/${id}`)).status;
              return { status, un: undefined, big: 2n ** 70n, when: new Date(86400000), m: new Map([[1, { a: NaN }], ["k", -0]]), s: new Set([1, "x"]),
                buf: Buffer.from([0, 255]), u16: new Uint16Array([1, 65535]), re: /a+/gi, $dollar: 1, nul: Object.assign(Object.create(null), { z: [Infinity] }),
                here: process.cwd() + "/data", text: "ünï😀", nested: [[], {}, [undefined]] };
            }
            """}, call="(await lib.get(i)).status")
        self.scan()
        self.harden("lib.get")
        raw = (self.d / "calls.json").read_text()
        calls = cassette.load(self.d / "calls.json")
        self.assertEqual(len(calls), 3)
        r = calls[0]["result"]["$dict"] if "$dict" in calls[0]["result"] else calls[0]["result"]
        r = {(k if isinstance(k, str) else k): v for k, v in r}
        self.assertEqual(r["un"], {"$undefined": 0})
        self.assertEqual(r["big"], {"$bigint": str(2 ** 70)})
        self.assertEqual(r["when"], {"$datetime": "1970-01-02T00:00:00.000Z"})
        self.assertEqual(r["u16"], {"$typed": ["Uint16Array", "AQD//w=="]})
        self.assertEqual(r["here"], {"$proj": ["${PROJECT}", "${PROJECT}/data"]})
        self.assertNotIn(os.getcwd(), raw)
        self.assertEqual(absolute_paths(Path.cwd()), [])
        v = self.use((self.hardened().read_text()))  # the original's own code: every value equal after a round trip
        self.assertEqual((v["happy"]["equal"], v["happy"]["total"]), (3, 3), v.get("fatal"))

    def test_errors_keep_name_message_and_code(self):
        self.project({"lib.mjs": """
            import { execFileSync } from "node:child_process";
            export class OrderError extends Error { constructor(m) { super(m); this.name = "OrderError"; this.code = "E_ORDER"; } }
            export async function get(id) {
              const res = await fetch(`${process.env.API}/missing/${id}`);
              if (id === 0) throw new OrderError("status " + res.status);
              if (id === 1) throw new TypeError("bad");
              execFileSync("sh", ["-c", "exit 3"]);
            }
            """}, call="await lib.get(i).catch((e) => e.name)")
        self.scan()
        self.harden("lib.get")
        e = [c["exc"] for c in cassette.load(self.d / "calls.json")]
        self.assertEqual([(x["type"], x["msg"], x.get("code")) for x in e[:2]], [("OrderError", "status 404", "E_ORDER"), ("TypeError", "bad", None)])
        self.assertEqual((e[2]["type"], e[2]["status"]), ("Error", 3))
        v = self.use(self.hardened().read_text().replace('"TypeError', '"TypeError'))
        self.assertEqual((v["happy"]["equal"], v["happy"]["total"]), (3, 3), v.get("fatal"))
        v = self.use(self.hardened().read_text().replace('new TypeError("bad")', 'new RangeError("bad")'))
        self.assertFalse(v["gates"]["happy"])

    def test_concurrent_calls_keep_their_own_io(self):
        self.project({"lib.mjs": """
            export async function get(id) {
              await new Promise((r) => setTimeout(r, 20 - id * 5));
              const a = await (await fetch(`${process.env.API}/a/${id}`)).json();
              const b = await (await fetch(`${process.env.API}/b/${id}`)).json();
              return [a.path, b.path];
            }
            """}, call="await Promise.all([0, 1, 2, 3].map(lib.get))")
        self.scan()
        self.harden("lib.get")
        for c in cassette.load(self.d / "calls.json"):
            self.assertEqual([x["req"]["key"].rsplit("/", 2)[-2:] for x in c["io"]], [["a", str(c["args"][0])], ["b", str(c["args"][0])]])
        self.assertEqual(self.use(self.hardened().read_text())["happy"]["equal"], 12)


@needs_node
class VerifyControlsTest(Loop):
    """Hardenings that must be refused, next to one that must pass."""

    @classmethod
    def setUpClass(cls):
        cls.case = cls("setUpClass")
        cls.case.project({"lib.mjs": GET})
        cls.case.scan()
        cls.case.harden("lib.get")

    @classmethod
    def tearDownClass(cls):
        cls.case.doCleanups()

    def use(self, code):
        return Loop.use(self.case, code)

    def test_good_passes(self):
        v = self.use(GET_GOOD)
        self.passes(v)
        self.assertEqual((v["hardened"]["availability"], v["hardened"]["hang"]), (1.0, 0))
        self.assertGreater(v["original"]["hang"], 0)

    def test_rejects_the_unchanged_original(self):
        v = self.use(GET)
        self.assertFalse(v["passed"])
        self.assertFalse(v["gates"]["availability"])
        self.assertFalse(v["gates"]["no_hang"])

    def test_rejects_a_swallow(self):
        for body in ("try { return await (await fetch(u, { signal: AbortSignal.timeout(1000) })).json(); } catch { return null; }",
                     "return fetch(u, { signal: AbortSignal.timeout(1000) }).then((r) => r.json()).catch(() => undefined);"):
            v = self.use("export async function get(id) { const u = `${process.env.API}/items/${id}`; %s }" % body)
            self.assertFalse(v["passed"])
            self.assertFalse(v["gates"]["no_wrong"])
            # declaring it isn't enough: it has to be logged too
            v = self.use("export const RAS_FALLBACK = () => null; export async function get(id) { const u = `${process.env.API}/items/${id}`;"
                         "try { const r = await fetch(u, { signal: AbortSignal.timeout(1000) }); if (!r.ok) return null; return await r.json(); } catch { return null; } }")
            self.assertFalse(v["gates"]["no_wrong"])
            self.assertIn("without logging", v["chaos"]["first_failure"]["hardened"]["note"])

    def test_accepts_an_earned_logged_fallback(self):
        v = self.use("""
            import { logEvent, raiseForStatus } from "./_runtime.cjs";
            export const RAS_FALLBACK = (id) => ({ path: "unavailable", id });
            export async function get(id) {
              try { return await (await raiseForStatus(await fetch(`${process.env.API}/items/${id}`, { signal: AbortSignal.timeout(4000) }))).json(); }
              catch (e) { logEvent("get.degraded", { id, error: e }); return RAS_FALLBACK(id); }
            }""")
        self.passes(v)
        self.assertGreater(v["hardened"]["degraded"], 0)

    def test_rejects_a_fake_fallback_log(self):
        v = self.use("""
            import { logEvent } from "./_runtime.cjs";
            export const RAS_FALLBACK = () => null;
            let n = 0;
            export async function get(id) {
              if (n++ >= 3) { logEvent("get.degraded", { id, error: "pretend" }); return null; }
              return (await fetch(`${process.env.API}/items/${id}`, { signal: AbortSignal.timeout(1000) })).json();
            }""")
        self.assertFalse(v["passed"])
        self.assertFalse(v["gates"]["no_wrong"])
        self.assertIn("no dependency request failed", v["chaos"]["first_failure"]["hardened"]["note"])

    def test_rejects_throwing_a_good_answer_away(self):
        v = self.use("""
            import { logEvent, retry, raiseForStatus } from "./_runtime.cjs";
            export const RAS_FALLBACK = () => null;
            export async function get(id) {
              let failed = false;
              const out = await retry(async ({ signal }) => { try { const r = await raiseForStatus(await fetch(`${process.env.API}/items/${id}`, { signal })); return await r.json(); }
                catch (e) { failed = true; throw e; } }, { attempts: 5, timeoutMs: 1000, budgetMs: 4800, retryOn: () => true });
              if (failed) { logEvent("get.degraded", { id }); return null; }
              return out;
            }""")
        self.assertFalse(v["gates"]["no_wrong"])
        self.assertIn("answered in the end", v["chaos"]["first_failure"]["hardened"]["note"])

    def test_rejects_changed_output_and_mutated_arguments(self):
        v = self.use(GET.replace("await res.json()", "{ ...(await res.json()), extra: 1 }"))
        self.assertFalse(v["gates"]["happy"])
        v = self.use(GET_GOOD.replace("return await retry", "return await retry").replace(".json()", ".json().then((j) => ({ ...j, sent: undefined }))"))
        self.assertFalse(v["gates"]["happy"])

    def test_rejects_a_hang_it_tries_to_hide(self):
        v = self.use(GET_GOOD.replace("{ signal }) => (await", "{}) => (await").replace(", { signal }", "").replace("timeoutMs: 1500, budgetMs: 4700, ", ""))
        self.assertFalse(v["gates"]["no_hang"])

    def test_rejects_requests_the_original_never_made_even_if_caught(self):
        v = self.use(GET_GOOD.replace("try {", "try { await fetch(`${process.env.API}/health`).catch(() => {});", 1))
        self.assertFalse(v["gates"]["happy"])
        self.assertEqual(v["happy"]["first_failure"]["class"], "miss")
        v = self.use(GET_GOOD.replace("try {", "try { const net = await import('node:net'); await new Promise((r) => net.connect(80, 'example.com').on('error', r).on('connect', r)).catch(() => {});", 1))
        self.assertFalse(v["passed"])

    def test_rejects_background_failures_and_new_crashes(self):
        v = self.use(GET_GOOD.replace("try {", "try { Promise.reject(new Error('nobody listens'));", 1))
        self.assertFalse(v["gates"]["happy"])
        self.assertIn("nobody listens", v["happy"]["first_failure"]["exc"])
        v = self.use(GET_GOOD.replace('throw new RasError', 'null.x; throw new RasError').replace("attempts: 4", "attempts: 1"))
        self.assertFalse(v["gates"]["no_new_crashes"])
        self.assertEqual(v["chaos"]["new_crashes"], ["TypeError"])

    def test_rejects_over_budget_retries(self):
        v = self.use(GET_GOOD.replace("timeoutMs: 1500, budgetMs: 4700", "timeoutMs: 6000"))
        self.assertFalse(v["gates"]["no_hang"])

    def test_rejects_code_that_reaches_into_the_harness_or_does_not_load(self):
        v = self.use("import m from 'node:module';\n" + js(GET_GOOD))
        self.assertIn("_runtime.cjs only", v["fatal"]["message"])
        v = self.use("await null;\n" + js(GET_GOOD))
        self.assertIn("import hardened.mjs", v["fatal"]["stage"])
        v = self.use("export const other = 1;")
        self.assertIn("no function get", v["fatal"]["message"])
        v = self.use("export async function get( {")
        self.assertIn("import hardened.mjs", v["fatal"]["stage"])
        self.assertNotIn(os.getcwd(), (self.case.d / "verify.json").read_text())


CART = """
    export async function checkout(id) {
      const item = await (await fetch(`${process.env.API}/items/${id}`)).json();
      const res = await fetch(`${process.env.API}/orders`, { method: "POST", body: JSON.stringify({ id, path: item.path }) });
      return (await res.json()).sent;
    }
    """
CART_HEAD = """
    import { isTransient, raiseForStatus, RasError, retry } from "./_runtime.cjs";
    const opts = { attempts: 4, timeoutMs: 1100, budgetMs: 2300, retryOn: (e) => isTransient(e) || e instanceof SyntaxError };
    """


@needs_node
class IdempotencyTest(Loop):
    def setUp(self):
        self.project({"lib.mjs": CART}, call="await lib.checkout(i)")
        self.scan()
        self.harden("lib.checkout")

    def test_rejects_retrying_post_without_a_key(self):
        v = self.use(CART_HEAD + """
            export async function checkout(id) {
              try {
                return await retry(async ({ signal }) => {
                  const item = await (await raiseForStatus(await fetch(`${process.env.API}/items/${id}`, { signal }))).json();
                  const res = await fetch(`${process.env.API}/orders`, { signal, method: "POST", body: JSON.stringify({ id, path: item.path }) });
                  return (await (await raiseForStatus(res)).json()).sent;
                }, { ...opts, budgetMs: 4700 });
              } catch (e) { throw new RasError("checkout failed", { cause: e }); }
            }""")
        self.assertTrue(v["gates"]["happy"])
        self.assertFalse(v["gates"]["dups"])
        self.assertEqual(v["original"]["dups"], 0)
        self.assertGreater(v["hardened"]["dups"], 0)
        self.assertIn("timeout (per-key)", v["chaos"]["by_scenario"])

    def test_rejects_a_background_retry_of_the_post(self):
        v = self.use(CART_HEAD + """
            export async function checkout(id) {
              const item = await retry(async ({ signal }) => (await raiseForStatus(await fetch(`${process.env.API}/items/${id}`, { signal }))).json(), opts);
              const post = (signal) => fetch(`${process.env.API}/orders`, { signal, method: "POST", body: JSON.stringify({ id, path: item.path }) });
              try { return (await (await raiseForStatus(await post(AbortSignal.timeout(1000)))).json()).sent; }
              catch (e) { setTimeout(() => post(AbortSignal.timeout(1000)).catch(() => {}), 500); throw new RasError("later", { cause: e }); }
            }""")
        self.assertFalse(v["gates"]["dups"])

    def test_accepts_one_key_per_operation(self):
        v = self.use(CART_HEAD + """
            export async function checkout(id) {
              try {
                const item = await retry(async ({ signal }) => (await raiseForStatus(await fetch(`${process.env.API}/items/${id}`, { signal }))).json(), opts);
                const body = JSON.stringify({ id, path: item.path });
                const doc = await retry(async ({ signal }) => (await raiseForStatus(await fetch(`${process.env.API}/orders`,
                  { signal, method: "POST", body, headers: { "Idempotency-Key": `order-${id}` } }))).json(), opts);
                return doc.sent;
              } catch (e) { throw new RasError("checkout failed", { cause: e }); }
            }""")
        self.passes(v)
        self.assertEqual(v["hardened"]["dups"], 0)
        self.assertGreater(v["hardened"]["availability"], v["original"]["availability"])


SYNC = {"lib.cjs": """
    "use strict";
    const { execFileSync } = require("node:child_process");
    const conf = require("./conf.cjs");
    const PREFIX = conf.prefix.trim() + " ";
    function version() { return PREFIX + execFileSync("printf", ["v1.2\\n"], { encoding: "utf8" }).trim(); }
    module.exports = { version };
    """, "conf.cjs": "exports.prefix = 'git ';"}
SYNC_GOOD = """
    "use strict";
    const { execFileSync } = require("node:child_process");
    const { prefix } = require("../conf.cjs");
    const { RasError, retrySync, scope } = require("./_runtime.cjs");
    const { PREFIX } = scope("lib.version");
    function version() {
      try {
        return retrySync(({ timeoutMs }) => {
          const out = execFileSync("printf", ["v1.2\\n"], { encoding: "utf8", timeout: timeoutMs });
          if (!out.endsWith("\\n")) throw Object.assign(new Error("output cut off"), { code: "EPIPE" });
          return (prefix === PREFIX ? PREFIX : "?") + out.trim();
        }, { attempts: 4, timeoutMs: 1500, budgetMs: 4700 });
      } catch (e) { throw new RasError("version failed", { cause: e }); }
    }
    module.exports = { version, RAS_FALLBACK: null };
    """


@needs_node
class SwapTest(Loop):
    """enable/disable: byte for byte, kill switch, fails safe."""

    def check(self, files, lib, call, fid, good, after=None):
        p = self.project(files, lib, call)
        clean = run_app().stdout
        self.assertEqual(len(clean.splitlines()), 3)
        src = p.dir / lib
        before = src.read_bytes()
        self.scan()
        self.harden(fid)
        self.passes(self.use(good))
        with quiet():
            enable(fid, quiet=True)
        self.assertIn(b"// >>> ras-anything:%s >>>" % fid.encode(), src.read_bytes())
        on = run_app()
        self.assertEqual((on.stdout, on.stderr), (clean, ""))
        copy = next(x for x in (p.dir / "_ras_hardened").iterdir() if x.is_file() and x.name != "_runtime.cjs")
        marker = copy.read_text()
        # the hardened code is what runs; the kill switch runs the original; a broken copy falls back, loudly
        copy.write_text(marker.replace("RasError", "RasError", 1) + "\nprocess.stderr.write('HARDENED LOADED\\n');\n")
        self.assertIn("HARDENED LOADED", run_app().stderr)
        off = run_app({"RAS_ANYTHING_DISABLE": "1"})
        self.assertEqual((off.stdout, off.stderr), (clean, ""))
        copy.write_text("throw new Error('broken copy');")
        broken = run_app()
        self.assertEqual(broken.stdout, clean)
        self.assertIn("hardened %s not loaded, running the original" % fid, broken.stderr)
        copy.unlink()
        self.assertEqual(run_app().stdout, clean)
        copy.write_text(marker)
        if after:
            after(p, src)
        with quiet():
            r = disable(fid, quiet=True)
        self.assertTrue(r["restored"])
        self.assertEqual(src.read_bytes(), before)
        self.assertFalse((p.dir / "_ras_hardened").exists())
        return p

    def test_mjs_function(self):
        self.check({"lib.mjs": GET}, "lib.mjs", "await lib.get(i)", "lib.get", GET_GOOD)

    def test_mjs_arrow_const_without_final_newline(self):
        def const_again(p, src):
            self.assertIn("export let get = async", src.read_text())

        self.check({"lib.mjs": "export const get = async (id) => {\n  const res = await fetch(`${process.env.API}/items/${id}`);\n  return res.json();\n};"},
                   "lib.mjs", "await lib.get(i)", "lib.get", GET_GOOD, const_again)

    def test_cjs_function(self):
        self.check(SYNC, "lib.cjs", "lib.version()", "lib.version", SYNC_GOOD)

    def test_cjs_inline_export_and_crlf(self):
        files = {"lib.cjs": "const p = '/items/';\r\nexports.get = async (id) => {\r\n  const res = await fetch(process.env.API + p + id);\r\n  return res.json();\r\n};\r\n"}
        good = js(GET_GOOD).replace("import {", "const {").replace('} from "./_runtime.cjs"', '} = require("./_runtime.cjs")').replace("export async", "async") + \
            "\nmodule.exports = { get };\n"
        self.check({"lib.cjs": files["lib.cjs"].replace("\r\n", "\r\n")}, "lib.cjs", "await lib.get(i)", "lib.get", good)

    def test_js_in_a_module_package(self):
        self.check({"lib.js": GET, "package.json": '{"type": "module"}'}, "lib.js", "await lib.get(i)", "lib.get", GET_GOOD)

    @needs_ts
    def test_ts_function_and_const(self):
        good = js(GET_GOOD).replace("get(id)", "get(id: number): Promise<any>")
        self.check({"lib.ts": "interface Item { path: string }\nexport async function get(id: number): Promise<Item> {\n"
                              "  const res = await fetch(`${process.env.API}/items/${id}`);\n  return (await res.json()) as Item;\n}\n",
                    "package.json": '{"type": "module"}'}, "lib.ts", "await lib.get(i)", "lib.get", good)
        self.assertEqual(self.hardened().name, "hardened.mts")
        self.check({"lib.mts": "type Item = { path: string };\nexport const get = async (id: number): Promise<Item> => {\n"
                               "  const res = await fetch(`${process.env.API}/items/${id}`);\n  return (await res.json()) as Item;\n};\n"},
                   "lib.mts", "await lib.get(i)", "lib.get", good)

    @needs_ts
    def test_ts_stub_runs_as_it_is(self):
        self.project({"lib.mts": """
            interface Item { path: string; tags: Tag[] }
            type Tag = "a" | "b";
            const BASE: string = "/items/";
            const load = async (p: string): Promise<Item> => (await (await fetch(process.env.API + p)).json()) as Item;
            export const get = async (id: number, opt?: { deep: boolean }): Promise<Item["path"]> => (await load(BASE + id)).path;
            """}, "lib.mts")
        self.scan()
        self.harden("lib.get")
        v = self.use(self.hardened().read_text())
        self.assertEqual((v["happy"]["equal"], v["happy"]["total"]), (3, 3), v.get("fatal"))

    def test_two_swaps_in_one_file_come_out_in_any_order(self):
        two = js(GET) + "export const two = async (id) => { const r = await fetch(`${process.env.API}/two/${id}`); return r.json(); };\n"
        p = self.project({"lib.mjs": two}, call="[await lib.get(i), await lib.two(i)]")
        clean, before = run_app().stdout, (p.dir / "lib.mjs").read_bytes()
        self.scan()
        for fid, code in (("lib.get", GET_GOOD), ("lib.two", GET_GOOD.replace("function get", "function two").replace("/items/", "/two/"))):
            self.harden(fid)
            self.passes(self.use(code))
            enable(fid, quiet=True)
        self.assertEqual(run_app().stdout, clean)
        disable("lib.get", quiet=True)
        self.assertEqual(run_app().stdout, clean)
        self.assertTrue(disable("lib.two", quiet=True)["restored"])
        self.assertEqual((p.dir / "lib.mjs").read_bytes(), before)

    def test_hardening_next_to_an_enabled_function(self):
        two = js(GET) + "export async function two(id) { const g = await get(id); const r = await fetch(`${process.env.API}/two/${id}`); return [g, await r.json()]; }\n"
        self.project({"lib.mjs": two}, call="await lib.two(i)")
        clean = run_app().stdout
        self.scan()
        self.harden("lib.get")
        self.passes(self.use(GET_GOOD))
        enable("lib.get", quiet=True)
        self.scan()
        self.harden("lib.two")
        v = self.use(self.hardened().read_text())
        self.assertEqual((v["happy"]["equal"], v["happy"]["total"]), (3, 3), v.get("fatal"))
        self.assertEqual(run_app().stdout, clean)

    def test_symlinks_are_refused(self):
        p = self.project({"lib.mjs": GET})
        self.scan()
        self.harden("lib.get")
        self.passes(self.use(GET_GOOD))
        src, real = p.dir / "lib.mjs", p.dir / "real.mjs"
        shutil.move(src, real)
        src.symlink_to(real)
        with self.assertRaises(UserError) as cm:
            enable("lib.get", quiet=True)
        self.assertIn("symlink", cm.exception.what)
        src.unlink()
        shutil.move(real, src)
        out = Path(p.tmp) / "out"
        out.mkdir()
        (p.dir / "_ras_hardened").symlink_to(out)
        self.assertRaises(UserError, enable, "lib.get", quiet=True)
        (p.dir / "_ras_hardened").unlink()
        (p.dir / "_ras_hardened").mkdir()
        (p.dir / "_ras_hardened" / "_runtime.cjs").symlink_to(out / "x.cjs")
        self.assertRaises(UserError, enable, "lib.get", quiet=True)
        self.assertEqual(list(out.iterdir()), [])
        self.assertNotIn("ras-anything", src.read_text())

    def test_damaged_patches_in_state_are_refused(self):
        p = self.project({"lib.mjs": "export const get = async (id) => {\n  const r = await fetch(`${process.env.API}/items/${id}`);\n  return r.json();\n};\n"})
        self.scan()
        self.harden("lib.get")
        self.passes(self.use(GET_GOOD))
        enable("lib.get", quiet=True)
        sp = Path(".ras/state.json")
        good, src = sp.read_text(), (p.dir / "lib.mjs").read_text()
        for bad in ([["import os", "import evil"]], [["const a", "let b"]], "x", [["const a"]], [["const a\nb", "let a\nb"]]):
            st = json.loads(good)
            st["enabled"]["lib.get"]["patches"] = bad
            sp.write_text(json.dumps(st))
            self.assertRaises(UserError, disable, "lib.get", quiet=True)
            self.assertEqual((p.dir / "lib.mjs").read_text(), src)
        sp.write_text(good)
        self.assertTrue(disable("lib.get", quiet=True)["restored"])


@needs_node
class BenchTest(Loop):
    def test_bench_measures_and_gives_no_credit_to_a_lying_fallback(self):
        p = self.project({"lib.mjs": GET})
        self.scan()
        self.harden("lib.get")
        self.passes(self.use(GET_GOOD))
        enable("lib.get", quiet=True)
        res = bench(seeds=6, rate=0.2, quiet=True)
        self.assertTrue(res["clean_outputs_equal"])
        self.assertGreater(res["summary"]["hardened"]["availability"], res["summary"]["original"]["availability"])
        self.assertLess(res["functions"]["hardened"]["lib.get"]["raised"], res["summary"]["original"]["crash"] + res["summary"]["original"]["hang"])
        self.assertTrue(res["host"]["runtime"].startswith("Node.js "))
        (p.dir / "_ras_hardened" / "lib__get.mjs").write_text(js("""
            export const RAS_FALLBACK = () => null;
            export async function get(id) { process.stderr.write('{"trace_id": "x", "event": "fallback"}\\n'); return null; }
            """))
        res = bench(seeds=2, quiet=True)
        self.assertEqual(res["functions"]["hardened"]["lib.get"]["fallback"], 0)
        self.assertEqual(res["summary"]["hardened"]["wrong"], 2)


if __name__ == "__main__":
    unittest.main()
