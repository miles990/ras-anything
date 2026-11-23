"""Java adapter: launchers, boundaries, attribution, cassettes, verify's verdicts on good and bad hardenings, enable/disable."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ras_anything import cassette, faults, lang, runner
from ras_anything.bench import bench
from ras_anything.enable import disable, enable
from ras_anything.harden import harden
from ras_anything.lang import java_static as static
from ras_anything.paths import UserError
from ras_anything.scan import scan
from ras_anything.verify import verify
from tests.holdout import skip_unless_holdout
from tests.test_pipeline import FIX, REPO, Project, quiet

JDK = bool(shutil.which("java") and shutil.which("javac"))
HOLDOUT_JAVA = REPO / "wow" / "work" / "holdout" / "java"
CMD = ["java", "-Xmx200m", "-Dx=y", "-cp", "classes", "shop.Main", "one"]

HEAD = """package shop;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse.BodyHandlers;
import java.time.*;
import java.util.List;
import java.util.Map;
import static java.lang.Integer.parseInt;
import shop._ras_hardened.RasRt;

final class Hardened {
"""

GOOD = HEAD + """
    private static RasRt.Policy to(String name, int ms) {
        return RasRt.policy(name).deadline(Duration.ofMillis(ms)).attemptTimeout(Duration.ofMillis(ms / 2)).attempts(4).alsoRetry(e -> "bad body".equals(e.getMessage()));
    }

    private static final RasRt.Policy STOCK = to("stock", 2400), ORDERS = to("orders", 2400);

    private static HttpRequest.Builder to(String url) {
        return HttpRequest.newBuilder(URI.create(url));
    }

    public static Order order(String base, String sku, List<Integer> lots) throws Exception {
        String stock = STOCK.call(a -> field(RasRt.check(HTTP.send(a.apply(to(base + "/stock?sku=" + sku)).build(), BodyHandlers.ofString())).body(), "stock"));
        String id = ORDERS.call("POST", RasRt.idempotencyKey("order", sku, lots), a -> field(RasRt.check(HTTP.send(
                a.apply(to(base + "/orders").POST(HttpRequest.BodyPublishers.ofString(sku + ":" + parseInt(stock)))).build(), BodyHandlers.ofString())).body(), "order"));
        return new Order(id, lots, 0.5);
    }
}
"""

# what the original does, with room for one change
PLAIN = HEAD + """
    static final Order RAS_FALLBACK = new Order("none", List.of(), 0);

    static Order once(String base, String sku, List<Integer> lots) throws Exception {
        String stock = field(HTTP.send(HttpRequest.newBuilder(URI.create(base + "/stock?sku=" + sku)).timeout(Duration.ofSeconds(1)).build(), BodyHandlers.ofString()).body(), "stock");
        var res = HTTP.send(HttpRequest.newBuilder(URI.create(base + "/orders")).timeout(Duration.ofSeconds(1))
                .POST(HttpRequest.BodyPublishers.ofString(sku + ":" + parseInt(stock))).build(), BodyHandlers.ofString());
        if (res.statusCode() != 201) throw new IOException("orders " + res.statusCode());
        return new Order(field(res.body(), "order"), lots, 0.5);
    }

    public static Order order(String base, String sku, List<Integer> lots) throws Exception {
        %s
    }
}
"""

BAD = {
    "swallow": ("no_wrong", "try { return once(base, sku, lots); } catch (Exception e) { return null; }"),
    "retry_post_without_key": ("dups", "for (int i = 0; ; i++) { try { return once(base, sku, lots); } catch (IOException e) { if (i == 3) throw e; Thread.sleep(50); } }"),
    "changed_output": ("happy", "Order o = once(base, sku, lots); return new Order(o.id().toLowerCase(), o.lots(), o.weight());"),
    "fake_fallback": ("no_wrong", "RasRt.logEvent(\"fallback\", Map.of(\"ras.dependency\", \"orders\")); return RAS_FALLBACK;"),
}


class CommandTest(unittest.TestCase):
    def test_detection(self):
        for cmd in (["java", "App.java"], ["java", "App.java", "--port", "1"], ["java", "-cp", "out:lib/*", "shop.Main"], ["/usr/bin/java", "-jar", "app.jar", "x"],
                    ["java", "-Xmx1g", "-Dk=v", "--class-path=out", "Main"], ["java", "--source", "21", "script"], ["java", "-m", "m/Main"]):
            self.assertEqual(lang.detect(cmd).name, "java", cmd)
        for cmd, hint in ((["java"], "java App.java"), (["java", "-version"], "java App.java"), (["mvn", "exec:java"], "target/classes"),
                          (["./gradlew", "run"], "build/classes"), (["javac", "App.java"], "")):
            with self.assertRaises(UserError) as cm:
                lang.detect(cmd)
            self.assertIn(hint, cm.exception.impact)

    def test_jvm_options_are_not_the_main_class(self):
        c = static.Command(["java", "--add-opens", "a/b=ALL-UNNAMED", "-Xmx1g", "--add-modules=java.sql", "-classpath", "x", "-ea", "com/x/Main", "-cp", "y"])
        self.assertEqual((c.kind, c.main, c.cp, c.args), ("class", "com.x.Main", "x", ["-cp", "y"]))
        self.assertEqual(c.jvm, ["--add-opens", "a/b=ALL-UNNAMED", "-Xmx1g", "--add-modules", "java.sql", "-ea"])
        c = static.Command(["java", "-Dx=App.java", "-jar", "dist/a.jar", "--jar", "z"])
        self.assertEqual((c.kind, c.main, c.args), ("jar", "dist/a.jar", ["--jar", "z"]))
        c = static.Command(["java", "--source", "21", "tool", "a"])
        self.assertEqual((c.kind, c.main, c.args), ("source", "tool", ["a"]))

    def test_what_cannot_be_followed_is_refused(self):
        a = lang.by_name("java")
        if JDK:
            for cmd in (["java", "-m", "m/Main"], ["java", "@args"], ["java", "-cp", "out", "no.such.Main"], ["java", "Missing.java"]):
                with tempfile.TemporaryDirectory() as d:
                    self.assertRaises(UserError, a.resolve, cmd, Path(d))

    def test_sources_are_read_past_comments_and_strings(self):
        self.assertEqual(static.package_of('// package a;\n/* package b; */\npackage c.d ;\nclass X { String s = "package e;"; }'), "c.d")
        self.assertEqual(static.py_index("a𝄞b", 3), 2)

    def test_patches_are_checked_before_undoing(self):
        a = lang.by_name("java")
        self.assertTrue(a.valid_patch("static int f(int a) {", "static int _ras_orig_f(int a) {"))
        for old, new in (("x", "y"), ("static int f(", "static int f("), ("f(", "_ras_orig_f(); evil(")):
            self.assertFalse(a.valid_patch(old, new))


@unittest.skipUnless(JDK, "needs a JDK")
class JavaPipelineTest(unittest.TestCase):
    fid = "shop.Shop.order"

    @classmethod
    def setUpClass(cls):
        cls.p = Project(FIX / "javashop")
        cls.src = cls.p.dir / "src" / "shop" / "Shop.java"
        with quiet():
            cls.scan = scan(CMD, seeds=1, quiet=True)
            harden(cls.fid, quiet=True)
        cls.fns = {f["id"]: f for f in cls.scan["functions"]}
        cls.adapter = lang.by_name("java")

    @classmethod
    def tearDownClass(cls):
        cls.p.close()

    def hardened(self, code):
        (self.p.dir / ".ras" / "hardens" / self.fid / "hardened.java").write_text(code)
        with quiet():
            return verify(self.fid, quiet=True)

    def run_app(self, cfg, **kw):
        cmd = self.adapter.resolve(CMD, self.p.dir)
        return runner.run(self.adapter, cmd, cfg, self.p.dir, timeout=60, root=self.p.dir, **kw)

    def test_scan_attributes_calls_and_explains_what_cannot_be_hardened(self):
        self.assertEqual(self.scan["lang"], "java")
        self.assertEqual({k: (f["calls"], f["boundary"]) for k, f in self.fns.items() if f["boundary"]},
                         {"shop.Shop.order": (3, 6), "shop.Shop.price": (3, 3), "shop.Shop.stamp": (3, 3), "shop.Shop.async": (3, 3),
                          "shop.Shop.pooled": (3, 3), "shop.Shop.gone": (1, 1), "shop.Shop.instance": (3, 3)})
        self.assertEqual(self.fns["shop.Shop.stamp"]["boundary_kinds"], {"subprocess": 3})
        self.assertTrue(self.fns[self.fid]["hardenable"])
        self.assertIn("instance method", self.fns["shop.Shop.instance"]["verdict"])
        self.assertIn("main()", self.fns["shop.Main.main"]["verdict"])

    def test_signals(self):
        self.assertEqual(sorted(self.fns[self.fid]["signals"]), ["no_logging", "no_retry", "no_timeout", "write_no_key"])
        self.assertIn("swallow", self.fns["shop.Shop.gone"]["signals"])
        self.assertNotIn("no_timeout", self.fns["shop.Shop.async"]["signals"])
        self.assertNotIn("no_timeout", self.fns["shop.Shop.stamp"]["signals"])

    def test_live_faults_follow_the_same_plan_as_every_language(self):
        for seed in (2, 5, 11):
            r = self.run_app({"mode": "chaos", "seed": seed, "rate": 0.5, "outage_rate": 0.1, "scale": 0.01, "hang_s": 0.3})
            plan = faults.Seeded(seed, 0.5, 0.1)
            self.assertTrue(r.data["attempts"])
            for a in r.data["attempts"]:
                self.assertEqual(a["fault"], plan.fault(a["key"], a["n"]), a)
                self.assertEqual(a["outage"], plan.outage(a["key"], a["n"]))

    def test_every_boundary_is_faulted_live(self):
        """Seeds chosen so that http (both clients) and processes get their fault: the observed failure names the function."""
        hit = {}
        for seed in range(1, 13):
            r = self.run_app({"mode": "chaos", "seed": seed, "rate": 0.6, "outage_rate": 0.0, "scale": 0.01, "hang_s": 0.3})
            for f in r.data["functions"]:
                for k in f["faults"]:
                    hit.setdefault(f["id"], set()).add(k)
            u = r.data["uncaught"]
            if u:
                self.assertTrue(u["where"].startswith("shop.Shop."), u)
                self.assertEqual(r.hang, u["hang"])
                self.assertEqual(r.code, 70 if u["hang"] else 1)
            else:
                self.assertEqual(r.code, 0)
        self.assertTrue(hit.get("shop.Shop.price"), hit)
        self.assertTrue(hit.get("shop.Shop.order"), hit)

    def test_the_agent_gets_out_of_the_way(self):
        out = self.p.dir / "plain"
        subprocess.run(["javac", "-d", str(out), *map(str, sorted((self.p.dir / "src").rglob("*.java")))], check=True, capture_output=True)
        r = subprocess.run(["java", "-cp", str(out), "shop.Main", "one"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout, self.run_app({"mode": "scan"}).stdout)
        shutil.rmtree(out)
        self.assertFalse(list(self.p.dir.rglob("*.class")))

    def test_cassette(self):
        calls = cassette.load(self.p.dir / ".ras" / "hardens" / self.fid / "calls.json")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1]["args"][1:], ["bee", [1, 2]])
        self.assertEqual(calls[1]["result"], {"id": "O-2", "lots": [1, 2], "weight": 0.5})
        self.assertEqual([x["req"]["key"] for x in calls[1]["io"]][0], "GET http://127.0.0.1/stock?sku=bee")
        self.assertRegex(calls[1]["io"][1]["req"]["key"], r"^POST http://127\.0\.0\.1/orders #[0-9a-f]{12}$")
        self.assertEqual(calls[1]["io"][1]["out"]["status"], 201)
        meta = json.loads((self.p.dir / ".ras" / "hardens" / self.fid / "meta.json").read_text())
        self.assertEqual(meta["globals"], {"shop.Shop#suffix": "-eu"})

    def test_every_kind_of_boundary_replays_on_the_virtual_clock(self):
        """The untouched stubs: same facts on both sides, and what each API's faults come to."""
        want = {"shop.Shop.price": {"timeout": "hang", "refused": "error", "http5xx": "error", "slow": "ok", "truncated": "error"},
                "shop.Shop.stamp": {"timeout": "error", "refused": "error", "http5xx": "error", "slow": "error", "truncated": "error"},
                "shop.Shop.async": {"timeout": "error", "refused": "error", "http5xx": "wrong", "slow": "error", "truncated": "wrong"},
                "shop.Shop.pooled": {"timeout": "error", "refused": "error", "http5xx": "wrong", "slow": "error", "truncated": "wrong"}}
        for fid, exp in want.items():
            with quiet():
                harden(fid, quiet=True)
                v = verify(fid, quiet=True)
            self.assertNotIn("fatal", v, v.get("fatal"))
            self.assertEqual(v["happy"]["equal"], 3, fid)
            rows = v["chaos"]["by_scenario"]
            for fault, cls in exp.items():
                for side in ("original", "hardened"):
                    self.assertEqual(rows[fault][side], {cls: 3}, (fid, fault, side))
            self.assertFalse(v["gates"]["availability"])

    def test_verify_accepts_a_good_hardening_and_the_swap_is_reversible(self):
        v = self.hardened(GOOD)
        self.assertTrue(v.get("passed"), v.get("fatal") or (v["gates"], v["chaos"]["first_failure"]))
        self.assertEqual(v["hardened"]["availability"], 1.0)
        self.assertEqual(v["hardened"]["dups"], 0)
        self.assertEqual(v["ax"]["leaks"], [])
        self.assertIsNotNone(v["outage"]["hardened"]["recovered_after_s"])
        self.assertLess(v["outage"]["hardened"]["amplification"], v["outage"]["original"]["amplification"] + 1)
        self.assertGreater(v["serviceability"]["hardened"]["diagnosis_accuracy"], 0.5)

        before = self.src.read_bytes()
        with quiet():
            enable(self.fid, quiet=True)
        text = self.src.read_text()
        self.assertIn("// ras-anything:start " + self.fid, text)
        self.assertTrue((self.src.parent / "_ras_hardened" / "RasRt.java").is_file())
        out = self.p.dir / "plain"
        try:
            subprocess.run(["javac", "-d", str(out), *map(str, sorted((self.p.dir / "src").rglob("*.java")))], check=True, capture_output=True)
            runs = [subprocess.run(["java", "-cp", str(out), "shop.Main", "one"], capture_output=True, text=True, timeout=60,
                                   env={**os.environ, "RAS_ANYTHING_DISABLE": x, "RAS_ANYTHING_HEALTH_FILE": str(self.p.dir / "health.json")}) for x in ("0", "1")]
            self.assertEqual(runs[0].stdout, runs[1].stdout)
            self.assertEqual([r.returncode for r in runs], [0, 0])
            self.assertEqual(json.loads((self.p.dir / "health.json").read_text())["functions"][self.fid]["calls"], 3)
            with quiet():  # verify still knows the original while the swap is in
                self.assertTrue(verify(self.fid, quiet=True)["passed"])
                b = bench(CMD, seeds=2, quiet=True)
            self.assertTrue(b["clean_outputs_equal"])
            self.assertEqual(b["functions"]["hardened"][self.fid]["fallback"], 0)
        finally:
            shutil.rmtree(out, ignore_errors=True)
            with quiet():
                d = disable(self.fid, quiet=True)
        self.assertTrue(d["restored"])
        self.assertEqual(self.src.read_bytes(), before)
        self.assertFalse((self.src.parent / "_ras_hardened").exists())

    def test_the_kill_switch_stops_the_hardened_code(self):
        """A bench run shows which code ran: only the hardened one sends Idempotency-Key."""
        self.assertTrue(self.hardened(GOOD)["passed"])
        with quiet():
            enable(self.fid, quiet=True)
        try:
            target = [{"id": self.fid, "module": "shop.Shop", "qualname": "order", "in_main": False, "file": str(self.src)}]
            for off in ("0", "1"):
                r = self.run_app({"mode": "bench", "seed": None, "rate": 0, "targets": target}, env_extra={"RAS_ANYTHING_DISABLE": off})
                keys = {a["idem"] for a in r.data["attempts"] if a["method"] == "POST"}
                self.assertEqual(None in keys, off == "1", keys)
                self.assertEqual(r.data["stats"][self.fid]["hardened"], off == "0")
        finally:
            with quiet():
                disable(self.fid, quiet=True)

    def test_verify_rejects_bad_hardenings(self):
        for name, (gate, body) in BAD.items():
            with self.subTest(name):
                v = self.hardened(PLAIN % body)
                self.assertNotIn("fatal", v, v.get("fatal"))
                self.assertFalse(v["passed"])
                self.assertFalse(v["gates"][gate], v["gates"])

    def test_hardened_code_that_cannot_run_is_explained(self):
        v = self.hardened(GOOD.replace("HTTP.send(a.apply(to(base + \"/stock", "NOPE.send(a.apply(to(base + \"/stock"))
        self.assertEqual(v["fatal"]["stage"], "compile hardened code")
        line = next(i for i, ln in enumerate(GOOD.splitlines(), 1) if "/stock?sku" in ln)
        self.assertIn("hardened.java:%d:" % line, v["fatal"]["message"])
        for bad, why in ((GOOD.replace("final class Hardened", "final class Other"), "Hardened"),
                         (GOOD.replace("return new Order(id", "_ras_agent.Agent.exit(0); return new Order(id"), "_ras_agent"),
                         (GOOD.replace("import java.util.Map;", "import java.lang.reflect.Method;"), "reflect"),
                         (GOOD.replace("return new Order(id", "new java.net.Socket(); return new Order(id"), "Socket")):
            v = self.hardened(bad)
            self.assertFalse(v["passed"])
            self.assertIn(why, v["fatal"]["message"])

    def test_nothing_machine_specific_is_stored(self):
        from tests.test_portable import absolute_paths
        self.assertEqual(absolute_paths(self.p.dir), [])


@unittest.skipUnless(JDK, "needs a JDK")
class SingleFileTest(unittest.TestCase):
    def test_java_App_java_keeps_working_with_the_swap_in(self):
        p = Project(FIX / "javasingle")
        try:
            src = p.dir / "App.java"
            before = src.read_bytes().replace(b"\n", b"\r\n")
            src.write_bytes(before)
            with quiet():
                res = scan(["java", "App.java", "q"], seeds=1, quiet=True)
                harden("App.hi", quiet=True)
            self.assertEqual(res["functions"][0]["id"], "App.hi")
            hp = p.dir / ".ras" / "hardens" / "App.hi" / "hardened.java"
            s = hp.read_text()
            line = next(ln for ln in s.splitlines() if "HttpClient.newHttpClient()" in ln)
            hp.write_text(s.replace(line, """        String b = P.call(a -> {
            String x = RasRt.check(HttpClient.newHttpClient().send(a.apply(HttpRequest.newBuilder(URI.create(base + "/hi"))).build(), HttpResponse.BodyHandlers.ofString())).body();
            if (!x.endsWith("!")) throw new RasRt.BadBodyException("cut");
            return x;
        });
        return b + n;""").replace("final class Hardened {", """final class Hardened {
    static final RasRt.Policy P = RasRt.policy("hi").attemptTimeout(java.time.Duration.ofMillis(1300)).attempts(4);"""))
            with quiet():
                v = verify("App.hi", quiet=True)
                self.assertTrue(v.get("passed"), v.get("fatal") or v["gates"])
                enable("App.hi", quiet=True)
            runs = [subprocess.run(["java", "App.java", "q"], capture_output=True, text=True, timeout=60, env={**os.environ, "RAS_ANYTHING_DISABLE": x}).stdout for x in "01"]
            self.assertEqual(runs, ["hi!1 42\n"] * 2)
            with quiet():
                self.assertTrue(disable("App.hi", quiet=True)["restored"])
            self.assertEqual(src.read_bytes(), before)
        finally:
            p.close()

    def test_jar(self):
        p = Project(FIX / "javasingle")
        try:
            subprocess.run(["javac", "-d", "k", "App.java"], check=True, capture_output=True)
            subprocess.run(["jar", "cfe", "app.jar", "App", "-C", "k", "."], check=True, capture_output=True)
            with quiet():
                res = scan(["java", "-jar", "app.jar"], seeds=1, quiet=True)
            self.assertEqual(res["functions"][0]["id"], "App.hi")
        finally:
            p.close()


@unittest.skipUnless(JDK, "needs a JDK")
class RuntimeTest(unittest.TestCase):
    """RasRt on its own (what an enabled app runs)."""

    PROBE = """
    import _ras_hardened.RasRt;
    import java.time.Duration;
    import java.util.*;
    public class Probe {
        static int n;
        static void p(Object o) { System.out.println(o); }
        public static void main(String[] a) throws Exception {
            p(RasRt.mask("bob@x.com card 4111 1111 1111 1111 token=abc Bearer abcdefghij 415-555-0100"));
            p(RasRt.plain("GET http://10.0.0.1:8/x failed in /home/me/a/b.txt", 200));
            p(RasRt.toJson(RasRt.parseJson("{\\"a\\": [1, 2.5, \\"x\\\\n\\", null, true]}")));
            try { RasRt.parseJson("{\\"a\\": [1"); } catch (RasRt.BadBodyException e) { p(RasRt.faultClass(e) + " " + RasRt.isTransient(e)); }
            for (int s : new int[] {429, 503, 404, 403, 409, 402, 501}) p(RasRt.faultClass(new RasRt.HttpStatusException(s, "", null)));
            RasRt.Policy q = RasRt.policy("q").backoff(Duration.ofMillis(1), Duration.ofMillis(2));
            p(q.call(x -> { if (++n < 3) throw new java.net.ConnectException("no"); return "ok " + n; }));
            n = 0;
            try { q.call(x -> { n++; throw new RasRt.HttpStatusException(404, "", null); }); } catch (RasRt.HttpStatusException e) { p("404 " + n); }
            n = 0;
            try { q.call("POST", null, x -> { n++; throw new java.net.ConnectException("no"); }); } catch (RasRt.RasError e) { p("post " + n + " " + e.problem().get("next_action") + " " + e.problem().get("retryable")); }
            Set<String> keys = new HashSet<>();
            p(q.call("POST", RasRt.idempotencyKey("a", 1), x -> { keys.add(x.headers().get("Idempotency-Key")); if (keys.size() > 0 && ++n < 3) throw new java.net.ConnectException("no"); return keys.size(); }));
            RasRt.CircuitBreaker b = new RasRt.CircuitBreaker("b", 2, Duration.ofMillis(30));
            b.failure(); b.failure();
            try { b.allow(); } catch (RasRt.CircuitOpenException e) { p(b.state() + " " + RasRt.faultClass(e) + " " + RasRt.health().get("status")); }
            Thread.sleep(40); b.allow(); p(b.state()); b.success(); p(b.state());
            try { RasRt.requireApproval("charge", Map.of()); } catch (RasRt.ApprovalRequiredException e) { p(e.problem().get("needs_human_authorization") + " " + e.problem().get("next_action")); }
            try { RasRt.run(Duration.ofMillis(100), new ProcessBuilder("sleep", "5")); } catch (RasRt.ProcessFailedException e) { p(RasRt.faultClass(e)); }
            p(RasRt.run(Duration.ofSeconds(5), new ProcessBuilder("cat"), "fed").stdout);
            RasRt.Policy c = RasRt.policy("c").attempts(1);
            p(c.callCached("k", Duration.ofSeconds(60), x -> "fresh"));
            p(c.callCached("k", Duration.ofSeconds(60), x -> { throw new java.net.ConnectException("no"); }));
        }
    }
    """

    def test_runtime(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "_ras_hardened").mkdir()
            shutil.copy(static.RUNTIME_SRC, Path(d) / "_ras_hardened" / "RasRt.java")
            (Path(d) / "Probe.java").write_text(self.PROBE)
            subprocess.run(["javac", "-Xlint:all,-serial", "-Werror", "-d", "o", "Probe.java"], cwd=d, check=True)
            r = subprocess.run(["java", "-cp", "o", "Probe"], cwd=d, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            "[email] card [card] token=[secret] Bearer [token] [phone]", "GET [url] failed in [path]", '{"a": [1, 2.5, "x\\n", null, true]}', "truncated true",
            "rate_limited", "unavailable", "invalid_request", "auth", "conflict", "quota", "unsupported", "ok 3", "404 1", "post 1 retry_later true", "1",
            "open circuit_open degraded", "half_open", "closed", "true escalate", "timeout", "fed", "fresh", "fresh"])
        recs = [json.loads(ln) for ln in r.stderr.splitlines()]
        self.assertTrue(recs)
        for rec in recs:
            for k in ("Timestamp", "SeverityText", "SeverityNumber", "EventName", "TraceId", "Body", "Resource", "Attributes"):
                self.assertIn(k, rec)
        self.assertIn("ras.dependency.gave_up", {x["EventName"] for x in recs})


class ShippingExampleTest(unittest.TestCase):
    EX = REPO / "examples" / "shipping-java"

    def test_committed_artifacts(self):
        for rel in ("src/shipping/Main.java", "tools/label.sh", "RAS-REPORT.md", "REPRODUCE.md", "docs/RUNBOOK.md", "ras-card.svg",
                    ".ras/scan.json", ".ras/bench.json", ".ras/slo.json", ".ras/state.json"):
            self.assertTrue((self.EX / rel).is_file(), rel)
        self.assertEqual((REPO / "docs" / "cards" / "shipping-java.svg").read_bytes(), (self.EX / "ras-card.svg").read_bytes())
        self.assertEqual(json.loads((self.EX / ".ras" / "state.json").read_text())["enabled"], {})
        self.assertFalse(list(self.EX.rglob("_ras_hardened")))
        for fid in ("shipping.Carrier.fetchRate", "shipping.Carrier.bookShipment", "shipping.Labels.labelCode"):
            d = self.EX / ".ras" / "hardens" / fid
            v = json.loads((d / "verify.json").read_text())
            self.assertTrue(v["passed"], fid)
            self.assertEqual(v["ax"]["leaks"], [])
            self.assertEqual(v["hardened_sha256"], hashlib.sha256((d / "hardened.java").read_bytes()).hexdigest())
            self.assertIn("=====", (d / "agent.log").read_text())  # written by a real agent run

    def test_readme_numbers_are_the_measured_ones(self):
        row = next(ln for ln in (REPO / "README.md").read_text().splitlines() if "(examples/shipping-java)" in ln)
        b = json.loads((self.EX / ".ras" / "bench.json").read_text())["summary"]
        for v, name in ((json.loads((self.EX / ".ras" / "hardens" / f / "verify.json").read_text()), f.rsplit(".", 1)[1])
                        for f in ("shipping.Carrier.fetchRate", "shipping.Carrier.bookShipment", "shipping.Labels.labelCode")):
            self.assertIn("%s %.1f%% → **%.1f%%**" % (name, 100 * v["original"]["availability"], 100 * v["hardened"]["availability"]), row)
        self.assertIn("%.1f%% → **%.1f%%**" % (100 * b["original"]["availability"], 100 * b["hardened"]["availability"]), row)
        self.assertIn("p99 %.2fs → %.2fs" % (b["original"]["p99_wall"], b["hardened"]["p99_wall"]), row)

    def test_nothing_private_is_committed(self):
        from tests.test_portable import absolute_paths
        self.assertEqual(absolute_paths(self.EX), [])
        secret = re.compile(r"(?<![\w.])/(?:workspace|home|tmp|root|Users)/|\b(?:sk|ghp|gho|xox[abprs]|AKIA)[-_A-Za-z0-9]{12,}|BEGIN [A-Z ]*PRIVATE KEY")
        for p in self.EX.rglob("*"):
            if p.is_file() and "runs" not in p.relative_to(self.EX).parts:
                self.assertIsNone(secret.search(p.read_text(encoding="utf-8")), p)

    @unittest.skipUnless(JDK, "needs a JDK")
    def test_the_committed_hardening_still_verifies(self):
        p = Project(self.EX)
        try:
            shutil.copytree(self.EX / ".ras", p.dir / ".ras", ignore=shutil.ignore_patterns("runs"))
            with quiet():
                v = verify("shipping.Labels.labelCode", quiet=True)
            self.assertTrue(v.get("passed"), v.get("fatal") or v["gates"])
            for k in ("original", "hardened"):
                self.assertEqual(v[k]["availability"], json.loads((self.EX / ".ras/hardens/shipping.Labels.labelCode/verify.json").read_text())[k]["availability"])
        finally:
            p.close()



@unittest.skipUnless(JDK, "needs a JDK")
class MavenGradleLayoutTest(unittest.TestCase):
    """Maven (target/classes) and Gradle (build/classes/java/main) compiled classpaths with src/main/java layout."""

    APP = """package com.acme;
import com.sun.net.httpserver.HttpServer;
import java.net.*;
import java.net.http.*;
public class App {
    static String fetch(String base) throws Exception {
        HttpClient c = HttpClient.newHttpClient();
        var r = c.send(HttpRequest.newBuilder(URI.create(base + "/ok")).GET().build(), HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() != 200) throw new java.io.IOException("http " + r.statusCode());
        return r.body();
    }
    public static void main(String[] args) throws Exception {
        var s = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        s.createContext("/ok", x -> { byte[] b = "hello".getBytes(); x.sendResponseHeaders(200, b.length); x.getResponseBody().write(b); x.close(); });
        s.start();
        try { System.out.println(fetch("http://127.0.0.1:" + s.getAddress().getPort())); } finally { s.stop(0); }
    }
}
"""

    def test_maven_and_gradle_class_dirs(self):
        for marker, out_rel in (("pom.xml", "target/classes"), ("build.gradle", "build/classes/java/main")):
            with tempfile.TemporaryDirectory() as td:
                proj = Path(td)
                src = proj / "src" / "main" / "java" / "com" / "acme" / "App.java"
                src.parent.mkdir(parents=True)
                src.write_text(self.APP, encoding="utf-8")
                (proj / marker).write_text("// layout marker\n", encoding="utf-8")
                (proj / out_rel).mkdir(parents=True)
                subprocess.run(["javac", "-d", str(proj / out_rel), str(src)], check=True, capture_output=True)
                cwd = os.getcwd()
                os.chdir(proj)
                try:
                    with quiet():
                        sc = scan(["java", "-cp", out_rel, "com.acme.App"], seeds=1, quiet=True)
                        h = harden("com.acme.App.fetch", quiet=True)
                    self.assertEqual(sc["lang"], "java")
                    self.assertEqual(sc["functions"][0]["id"], "com.acme.App.fetch")
                    self.assertEqual(h["recorded"], 1)
                finally:
                    os.chdir(cwd)


@unittest.skipUnless(JDK, "needs a JDK")
@skip_unless_holdout("java")
class JavaHoldoutPipelineTest(unittest.TestCase):
    GOOD_RAS = r"""
import java.io.*;
import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.util.Map;
import _ras_hardened.RasRt;

final class Hardened {
    private static final RasRt.Policy BEANS = RasRt.policy("beans")
            .deadline(Duration.ofMillis(2400)).attemptTimeout(Duration.ofMillis(1200)).attempts(4);
    private static final RasRt.Policy ROASTS = RasRt.policy("roasts")
            .deadline(Duration.ofMillis(2400)).attemptTimeout(Duration.ofMillis(1200)).attempts(4);

    static Result target(String base, String item) throws Exception {
        HttpClient c = HttpClient.newHttpClient();
        int stock = BEANS.call(a -> {
            var res = RasRt.check(c.send(a.apply(HttpRequest.newBuilder(URI.create(base + "/beans/" + item))).GET().build(), HttpResponse.BodyHandlers.ofString()));
            if (res.statusCode() != 200) throw new IllegalArgumentException("HTTP " + res.statusCode());
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) RasRt.parseJson(res.body());
            return ((Long) m.get("available")).intValue();
        });
        int n = Math.max(0, 10 - stock);
        String body = "{\"item\":\"" + item + "\",\"quantity\":" + n + "}";
        String oid = ROASTS.call("POST", RasRt.idempotencyKey("roast", item, n), a -> {
            var res = RasRt.check(c.send(a.apply(HttpRequest.newBuilder(URI.create(base + "/roasts"))).POST(HttpRequest.BodyPublishers.ofString(body)).build(), HttpResponse.BodyHandlers.ofString()));
            if (res.statusCode() != 200) throw new IOException("POST HTTP " + res.statusCode());
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) RasRt.parseJson(res.body());
            return (String) m.get("id");
        });
        return new Result(item, stock, n, oid);
    }
}
"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name) / "holdout_java"
        shutil.copytree(HOLDOUT_JAVA, self.proj)
        for c in self.proj.glob("*.class"):
            c.unlink()
        self._cwd = os.getcwd()
        os.chdir(self.proj)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_holdout_positive_and_four_bad_hardenings_rejected(self):
        orig_sha = hashlib.sha256((self.proj / "App.java").read_bytes()).hexdigest()
        with quiet():
            sc = scan(["java", "App.java"], seeds=2, quiet=True)
        self.assertEqual(sc["lang"], "java")
        self.assertEqual(sc["functions"][0]["id"], "App.target")
        with quiet():
            h = harden("App.target", quiet=True)
        self.assertEqual(h["recorded"], 1)
        hp = self.proj / ".ras" / "hardens" / "App.target" / "hardened.java"

        hp.write_text(self.GOOD_RAS, encoding="utf-8")
        with quiet():
            vg = verify("App.target", quiet=True)
        self.assertTrue(vg["passed"], vg.get("fatal") or vg.get("gates"))
        self.assertEqual(vg["ax"]["leaks"], [])
        self.assertGreater(vg["hardened"]["availability"], vg["original"]["availability"])

        with quiet():
            enable("App.target", quiet=True)
        r_en = subprocess.run(["java", "App.java"], cwd=self.proj, capture_output=True, text=True, check=True)
        r_dis = subprocess.run(["java", "App.java"], cwd=self.proj, env={**os.environ, "RAS_ANYTHING_DISABLE": "1"},
                               capture_output=True, text=True, check=True)
        self.assertEqual(r_en.stdout.strip(), r_dis.stdout.strip())
        with quiet():
            dis = disable("App.target", quiet=True)
        self.assertTrue(dis["restored"])
        self.assertEqual(hashlib.sha256((self.proj / "App.java").read_bytes()).hexdigest(), orig_sha)

        bads = {
            "swallow": ("no_wrong", "static Result target(String b, String i) throws Exception { return swallow(b, i); }"),
            "retryPost": ("dups", "static Result target(String b, String i) throws Exception { return retryPost(b, i); }"),
            "changeOutput": ("happy", "static Result target(String b, String i) throws Exception { return changeOutput(b, i); }"),
            "fakeFallback": ("no_wrong", "static Result target(String b, String i) throws Exception { return fakeFallback(b, i); }"),
        }
        for name, (gate, body) in bads.items():
            hp.write_text("final class Hardened {\n    " + body + "\n}\n", encoding="utf-8")
            with quiet():
                vb = verify("App.target", quiet=True)
            self.assertFalse(vb["passed"], f"holdout {name} unexpectedly passed")
            self.assertFalse(vb["gates"][gate], f"holdout {name} expected {gate}=False, got {vb['gates']}")


if __name__ == "__main__":
    unittest.main()
