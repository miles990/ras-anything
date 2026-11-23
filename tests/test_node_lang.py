"""Node.js adapter: which commands are ours, and what the source analysis says about a module."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ras_anything import faults, lang
from ras_anything.lang import node, node_static
from ras_anything.lang.base import LangAdapter
from ras_anything.paths import UserError
from tests.node_support import AGENT, js, needs_node, run_json

A = lang.by_name("node")


def info(text: str, name: str = "lib.mjs") -> node_static.FileInfo:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / name
        p.write_text(js(text))
        return node_static.FileInfo(str(p))


class DetectTest(unittest.TestCase):
    def test_node_launchers(self):
        for cmd in (["node", "app.js"], ["node", "--enable-source-maps", "app.mjs"], ["/usr/local/bin/node", "x.cjs"], ["nodejs", "a.ts"],
                    ["node22", "a.mts"], ["C:\\node\\node.exe", "a.js"]):
            self.assertEqual(lang.detect(cmd).name, "node", cmd)

    def test_bun_and_deno_get_a_clear_not_yet(self):
        for cmd, name in ((["bun", "run", "app.ts"], "Bun"), (["deno", "run", "-A", "app.ts"], "Deno")):
            with self.assertRaises(UserError) as cm:
                lang.detect(cmd)
            self.assertIn("%s isn't supported yet" % name, cm.exception.what)
            self.assertIn("ras-anything scan -- node app.ts", cm.exception.impact)

    def test_hints_for_wrappers(self):
        for prog in ("npx", "tsx", "ts-node"):
            with self.assertRaises(UserError) as cm:
                lang.detect([prog, "app.ts"])
            self.assertIn("ras-anything scan -- node app.ts", cm.exception.impact)

    def test_entry(self):
        for args, want in ((["app.js", "x"], "app.js"), (["--require", "x.cjs", "app.js"], "app.js"), (["--no-warnings", "a.mjs"], "a.mjs"),
                           (["-e", "1"], None), (["--import=tsx", "a.ts"], "a.ts"), ([], None), (["--", "a.js"], "a.js")):
            self.assertEqual(node.entry(args), want, args)

    def test_project_root_is_the_package(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "svc" / "src").mkdir(parents=True)
            (root / "svc" / "src" / "app.mjs").write_text("")
            self.assertEqual(A.project_root(["node", "svc/src/app.mjs"], root), root)
            (root / "svc" / "package.json").write_text("{}")
            self.assertEqual(A.project_root(["node", "svc/src/app.mjs"], root), root / "svc")
            self.assertEqual(A.project_root(["node", "src/app.mjs"], root / "svc"), root / "svc")

    def test_old_node_and_missing_typescript_support_are_errors(self):
        old = {"version": "20.1.0", "hooks": False, "esm": False, "ts": False}
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "node"
            exe.write_text("")
            with mock.patch.object(node, "probe", return_value=old), self.assertRaises(UserError) as cm:
                A.resolve([str(exe), "app.js"], Path(d))
            self.assertIn("20.1.0 is too old", cm.exception.what)
            with mock.patch.object(node, "probe", return_value={**old, "hooks": True, "esm": True}):
                with self.assertRaises(UserError) as cm:
                    A.resolve([str(exe), "app.ts"], Path(d))
                self.assertIn("--experimental-strip-types", cm.exception.impact)
                self.assertEqual(A.resolve([str(exe), "--experimental-strip-types", "app.ts"], Path(d))[-1], "app.ts")

    def test_adapter_is_complete(self):
        self.assertIsInstance(A, LangAdapter)
        for f in ("agent", "boundary", "faults", "fakes", "hooks", "runtime", "values", "worker"):
            self.assertTrue((AGENT / (f + ".cjs")).is_file(), f)


class TokenizerTest(unittest.TestCase):
    def names(self, text, name="lib.mjs"):
        return [f.qualname for f in info(text, name).funcs]

    def test_strings_comments_regex_and_templates(self):
        fi = info(r"""
            #!/usr/bin/env node
            // function commented() {}
            /* export function alsoCommented() {} */
            const s = "function inString() { '";
            const re = /[/"'`{]/g, half = 4 / 2 / 1;
            if (half) /}`/.test(s);
            export function real(a) { return `x ${a ? `y ${{ b: "}" }.b}` : "`"} z`; }
            export function after() { return 1; }
            """)
        self.assertEqual([f.qualname for f in fi.funcs], ["real", "after"])
        self.assertEqual([f.line for f in fi.funcs], [7, 8])

    def test_garbage_is_not_looked_into_rather_than_guessed(self):
        with tempfile.TemporaryDirectory() as d:
            for i, text in enumerate(("function f( {", "const a = `", "))) {{{", "\u0000", "const x = '")):
                p = Path(d) / ("x%d.js" % i)
                p.write_text(text)
                self.assertIsNone(node_static.load(str(p)))

    def test_statements_without_semicolons(self):
        fi = info("""
            export const a = async () => {
              return 1
            }
            export const b = async (x) =>
              x
                .then((y) => y)
            const c = 1
            export async function d() {}
            """)
        self.assertEqual([(f.qualname, f.is_async, bool(f.exports)) for f in fi.funcs], [("a", True, True), ("b", True, True), ("d", True, True)])
        self.assertIn("c", fi.vars)

    def test_typescript(self):
        fi = info("""
            import type { Thing } from "./types.ts";
            import { type Other, real } from "./real.ts";
            interface Order { id: string; items: { sku: string }[] }
            type Result = { ok: true } | { ok: false; why: string };
            export function over(a: string): string;
            export function over(a: number): string;
            export function over(a: any): string { return String(a); }
            export async function fetchOrder<T extends { id: string }>(id: string, opts?: { n: number }): Promise<{ a: T; b: Order[] }> {
              return real(id) as any;
            }
            export const go: (o: Order) => Promise<Result> = async (o: Order): Promise<Result> => { return { ok: true }; };
            declare function external(x: number): void;
            export abstract class Base<T> { protected abstract f(): void; async run(x: T): Promise<void> { } }
            """, "lib.ts")
        self.assertEqual([f.qualname for f in fi.funcs], ["over", "fetchOrder", "go", "Base.run"])
        self.assertTrue(fi.esm)
        go = fi.by_name["go"]
        self.assertEqual((go.binding, go.exports, go.is_async), ("const", ["go"], True))
        self.assertEqual(set(fi.uses(go)["types"]), {"Order", "Result"})
        self.assertEqual([v.source for v in fi.uses(fi.by_name["fetchOrder"])["imports"].values()], ["./real.ts"])

    def test_esm_or_commonjs(self):
        self.assertTrue(info("export const a = 1", "a.js").esm)
        self.assertFalse(info("exports.a = 1", "a.js").esm)
        self.assertTrue(info("1", "a.mjs").esm)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "package.json").write_text('{"type": "module"}')
            (Path(d) / "a.js").write_text("console.log(1)")
            self.assertTrue(node_static.FileInfo(str(Path(d) / "a.js")).esm)


class ExportsTest(unittest.TestCase):
    def reasons(self, text, name="lib.mjs"):
        fi = info(text, name)
        return {f.qualname: fi.refusal(f) for f in fi.funcs}

    def test_esm(self):
        r = self.reasons("""
            let counter = 0;
            const cache = new Map();
            const LIMIT = 5, TABLE = { a: 1 };
            export function plain() { return LIMIT + TABLE.a; }
            export async function asyncOne() {}
            export const arrow = async () => {};
            export const classic = function () {};
            function later() {} export { later, local as renamed };
            const local = () => 1;
            export default async function main() {}
            function hidden() {}
            export function counts() { return ++counter; }
            export function caches(k) { cache.set(k, 1); }
            export function viaHelper(k) { return caches(k); }
            export function usesThis() { return this.x; }
            export function where() { return import.meta.url; }
            export function* gen() {}
            const one = () => 1, two = () => 2; export { one, two };
            export const made = wrap(async () => {});
            """)
        ok = ["plain", "asyncOne", "arrow", "classic", "later", "local", "main"]
        self.assertEqual([k for k, v in r.items() if v is None], ok)
        self.assertIn("not exported", r["hidden"])
        self.assertIn("closure state: the module changes `counter`", r["counts"])
        self.assertIn("`cache`", r["caches"])
        self.assertIn("`cache` (through caches)", r["viaHelper"])
        self.assertIn("`this`", r["usesThis"])
        self.assertIn("import.meta", r["where"])
        self.assertIn("generator", r["gen"])
        self.assertIn("split the statement", r["one"])
        self.assertNotIn("made", r)

    def test_default_exported_values(self):
        r = self.reasons("async function main() {}\nexport default main;\n")
        self.assertIn("export default function main", r["main"])
        r = self.reasons("export default async function () {}\n")
        self.assertIn("anonymous", r["default"])

    def test_commonjs(self):
        fi = info("""
            "use strict";
            const http = require("node:http");
            async function a() { return __dirname; }
            const b = async (x) => x;
            function c() {}
            exports.d = async function () {};
            module.exports.e = () => 1;
            module.exports = { a, b: b, f() { return 1; }, async g(x) { return x; }, h: async () => 2, [http.x]: 3, ...others };
            """, "lib.cjs")
        r = {f.qualname: fi.refusal(f) for f in fi.funcs}
        self.assertEqual(sorted(k for k, v in r.items() if v is None), ["b", "d", "e", "f", "g", "h"])
        self.assertIn("__dirname", r["a"])
        self.assertIn("not exported", r["c"])
        self.assertEqual(fi.by_name["g"].accessor, "module.exports.g")
        self.assertEqual(fi.function_text(fi.by_name["g"]), "async function g(x) { return x; }")
        self.assertEqual(fi.function_text(fi.by_name["h"]), "const h = async () => 2;")

    def test_methods_are_seen_but_not_offered(self):
        r = self.reasons("""
            export class Client {
              #secret = 1;
              static async open(url) { return fetch(url); }
              async get(id) { return fetch(id); }
              get size() { return 1; }
              constructor(x) { this.x = x; }
              handler = async () => {};
            }
            """)
        self.assertEqual(set(r), {"Client.open", "Client.get"})
        self.assertIn("class method", r["Client.get"])


class StubTest(unittest.TestCase):
    def test_stub_brings_imports_helpers_and_scope(self):
        text = """
            import { readFile } from "node:fs/promises";
            import { thing } from "./sub/thing.mjs";
            import unused from "./unused.mjs";
            const API = process.env.API ?? "x";
            const helper = async (u) => fetch(`${API}${u}`);
            export function shared() {}
            export async function load(id) {
              shared();
              const m = await import("./lazy.mjs");
              return thing(await helper(id), readFile, m);
            }
            """
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "lib.mjs").write_text(js(text))
            s = A.stub({"id": "lib.load", "file": "lib.mjs", "qualname": "load", "name": "load", "async": True, "hardened_ext": ".mjs"}, Path(d))
        self.assertIn('import { thing } from "../sub/thing.mjs"', s)
        self.assertIn('await import("../lazy.mjs")', s)
        self.assertIn('import { readFile } from "node:fs/promises"', s)
        self.assertNotIn("unused", s)
        self.assertIn('const { API, shared } = scope("lib.load");', s)
        self.assertIn("const helper = async (u) =>", s)
        self.assertIn("export async function load(id)", s)
        self.assertIn("export const RAS_FALLBACK = null;", s)
        self.assertIn('from "./_runtime.cjs"', s)


class SignalsTest(unittest.TestCase):
    def signals(self, body, head="", name="f"):
        fi = info(head + "\n" + body)
        return fi.analyze(fi.by_name[name])

    def test_fetch(self):
        s = self.signals("export async function f(u) { const r = await fetch(u); return r.json(); }")
        self.assertEqual(set(s), {"no_timeout", "no_retry", "no_logging"})
        self.assertIn("fetch() without a timeout (signal)", s["no_timeout"])
        s = self.signals("export async function f(u) { return fetch(u, { signal: AbortSignal.timeout(500) }); }")
        self.assertNotIn("no_timeout", s)
        s = self.signals("export async function f(u) { const o = { signal: AbortSignal.timeout(5) }; return fetch(u, o); }")
        self.assertNotIn("no_timeout", s)

    def test_writes(self):
        s = self.signals("export async function f(u) { return fetch(u, { method: 'POST', body: '1' }); }")
        self.assertIn("POST without Idempotency-Key", s["write_no_key"])
        s = self.signals("export async function f(u) { return fetch(u, { method: 'POST', headers: { 'Idempotency-Key': u } }); }")
        self.assertNotIn("write_no_key", s)
        s = self.signals("""export async function f(u) { for (let i = 0; i < 3; i++) { try { return await fetch(u, { method: "POST" }); }
            catch (e) { console.error(e); } } }""")
        self.assertIn("retries a POST", s["unsafe_retry"])
        self.assertNotIn("no_retry", s)
        self.assertNotIn("no_logging", s)

    def test_a_loop_over_items_is_not_a_retry(self):
        s = self.signals("export async function f(us) { for (const u of us) await fetch(u); }")
        self.assertIn("no_retry", s)

    def test_swallowing(self):
        for body in ("try { return await fetch(u); } catch {}", "try { return await fetch(u); } catch (e) { return null; }",
                     "return fetch(u).catch(() => null);", "return fetch(u).catch((e) => {});", "return fetch(u).catch(function () { return []; });"):
            self.assertIn("swallow", self.signals("export async function f(u) { %s }" % body), body)
        for body in ("try { return await fetch(u); } catch (e) { throw e; }", "try { return await fetch(u); } catch (e) { console.error(e); return null; }",
                     "return fetch(u).catch(handle);", "return fetch(u).catch((e) => { throw new Error('x', { cause: e }); });",
                     "try { return await fetch(u); } catch (e) { last = e; }"):
            self.assertNotIn("swallow", self.signals("export async function f(u) { %s }" % body), body)

    def test_context(self):
        s = self.signals("export async function f(u) { try { return await fetch(u); } catch (e) { throw new Error('x'); } }")
        self.assertIn("no_context", s)
        s = self.signals("export async function f(u) { try { return await fetch(u); } catch (e) { throw new Error('x', { cause: e }); } }")
        self.assertNotIn("no_context", s)

    def test_http_and_child_processes(self):
        s = self.signals("export function f(u) { http.get(u, () => {}); }", "import http from 'node:http';")
        self.assertIn("http.get() without timeout", s["no_timeout"])
        s = self.signals("export function f(u) { get(u, { timeout: 5 }, () => {}); }", "import { get } from 'https';")
        self.assertNotIn("no_timeout", s)
        for call in ("execFileSync('git', ['x'])", "cp.spawnSync('git')", "run('git', ['x'])"):
            s = self.signals("export function f() { return %s; }" % call, "import * as cp from 'node:child_process'; import { execFileSync } from 'child_process';"
                             "import { promisify } from 'node:util'; const run = promisify(cp.execFile);")
            self.assertIn("without timeout", s["no_timeout"], call)
        s = self.signals("export function f() { return execFileSync('git', [], { timeout: 5 }); }", "import { execFileSync } from 'child_process';")
        self.assertNotIn("no_timeout", s)

    def test_helpers_count(self):
        s = self.signals("export async function f(u) { return helper(u); }\nasync function helper(u) { return fetch(u); }")
        self.assertIn("no_timeout", s)


class InstrumentTest(unittest.TestCase):
    def test_line_numbers_stay_and_the_prologue_is_respected(self):
        fi = info('"use strict";\nfunction a() {}\nconst b = () => 1;\nmodule.exports = { a, b };\n', "lib.cjs")
        out = node_static.instrument(fi, os.path.dirname(fi.path))["source"]
        self.assertEqual(out.count("\n"), fi.src.count("\n"))
        self.assertTrue(out.startswith('"use strict";;{const $w='))
        self.assertIn("let   b = () => 1;;{", out)


@needs_node
class PlanParityTest(unittest.TestCase):
    """The JS agent draws the same faults as Python for the same seed."""

    def test_same_schedule(self):
        keys = ["GET http://a/x?y=1", "POST http://b/ü #0123456789ab", "run git --version"]
        py = [[faults.Seeded(s, 0.3, 0.1, salt=t).fault(k, n) for n in range(8)] for s in (1, 7, 12345) for t in ("", "call3") for k in keys]
        js_ = run_json("""
            import { createRequire } from "node:module";
            const { plan } = createRequire(import.meta.url)(%r);
            const keys = %s;
            console.log(JSON.stringify([1, 7, 12345].flatMap((seed) => ["", "call3"].flatMap((salt) => keys.map((k) => {
              const f = plan({ type: "seeded", seed, rate: 0.3, outage_rate: 0.1, salt });
              return [...Array(8).keys()].map((n) => f(k, n));
            })))));
            """ % (str(AGENT / "faults.cjs"), __import__("json").dumps(keys)))
        self.assertEqual(js_, py)
        first = run_json("""
            import { createRequire } from "node:module";
            const { plan } = createRequire(import.meta.url)(%r);
            const o = plan({ type: "first", fault: "outage", key: null }), t = plan({ type: "first", fault: "timeout", key: "K" });
            console.log(JSON.stringify([[0, 1, 2, 3].map((n) => o("x", n)), [t("K", 0), t("K", 1), t("J", 0)]]));
            """ % str(AGENT / "faults.cjs"))
        self.assertEqual(first, [["refused"] * 3 + [None], ["timeout", None, None]])


class ForbiddenTest(unittest.TestCase):
    def test_hardened_code_must_not_reach_into_the_harness(self):
        bad = ["import m from 'node:module';", "import { Worker } from 'worker_threads';", "const b = require('vm');", "eval('1');",
               "process.getBuiltinModule('fs');", "require.cache['x'];", "globalThis.fetch = null;", "const p = '/x/ras_anything/lang/node_agent/boundary.cjs';",
               "globalThis[Symbol.for('ras-anything.agent')];", "new Function('x');", "process.env['RAS_ANYTHING_CFG'];"]
        good = ["import http from 'node:http';", "import { retry } from './_runtime.cjs';", "const module = 'module';", "const d = { cache: 1 }; d.cache;"]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "hardened.mjs"
            for line in bad:
                p.write_text(line)
                self.assertIsNotNone(A.refuse_hardened(p), line)
            for line in good:
                p.write_text(line)
                self.assertIsNone(A.refuse_hardened(p), line)


if __name__ == "__main__":
    unittest.main()
