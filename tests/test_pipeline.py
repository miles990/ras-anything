"""End to end on small copies of the example apps: scan -> harden -> verify (negative and positive
controls) -> enable/disable byte for byte. Offline: the apps run their own local upstream."""

import contextlib
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ras_anything.enable import disable, enable
from ras_anything.harden import harden
from ras_anything.scan import scan
from ras_anything.verify import verify

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
FIX = HERE / "fixtures"


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class Project:
    def __init__(self, src: Path):
        self.tmp = tempfile.mkdtemp(prefix="ras-test-")
        self.dir = Path(self.tmp) / src.name
        shutil.copytree(src, self.dir, ignore=shutil.ignore_patterns(".ras", "__pycache__", "_ras_hardened",
                                                                     "RAS-REPORT.md", "ras-card.svg", "REPRODUCE.md"))
        self.old = os.getcwd()
        os.chdir(self.dir)

    def close(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def use(self, fid, fixture):
        shutil.copyfile(FIX / fixture, self.dir / ".ras" / "hardens" / fid / "hardened.py")
        with quiet():
            return verify(fid, quiet=True)


class WeatherTest(unittest.TestCase):
    fid = "weather.fetch_forecast"

    @classmethod
    def setUpClass(cls):
        cls.p = Project(REPO / "examples" / "weather")
        with quiet():
            res = scan([sys.executable, "app.py"], seeds=1, quiet=True)
            harden(cls.fid, quiet=True)
        cls.scan = res

    @classmethod
    def tearDownClass(cls):
        cls.p.close()

    def test_scan_ranks_fetch_forecast_first(self):
        top = self.scan["functions"][0]
        self.assertEqual(top["id"], self.fid)
        self.assertTrue(top["hardenable"])
        self.assertIn("no_timeout", top["signals"])

    def test_rejects_swallow_everything_return_none(self):
        v = self.p.use(self.fid, "weather_swallow_none.py")
        self.assertFalse(v["passed"])
        self.assertFalse(v["gates"]["no_wrong"])
        self.assertGreater(v["hardened"]["wrong"], 0)

    def test_rejects_changed_happy_path(self):
        v = self.p.use(self.fid, "weather_changed_happy.py")
        self.assertFalse(v["passed"])
        self.assertFalse(v["gates"]["happy"])

    def test_rejects_the_unchanged_original(self):
        v = self.p.use(self.fid, "../../examples/weather/weather.py")
        self.assertFalse(v["passed"])
        self.assertFalse(v["gates"]["availability"])

    def test_good_passes_and_enable_disable_is_byte_for_byte(self):
        v = self.p.use(self.fid, "weather_good.py")
        self.assertTrue(v["passed"], v["gates"])
        self.assertEqual(v["hardened"]["hang"], 0)
        src = self.p.dir / "weather.py"
        before = hashlib.sha256(src.read_bytes()).hexdigest()
        clean = subprocess.run([sys.executable, "app.py"], capture_output=True, text=True).stdout
        with quiet():
            enable(self.fid, quiet=True)
        self.assertNotEqual(hashlib.sha256(src.read_bytes()).hexdigest(), before)
        self.assertIn("# >>> ras-anything:%s >>>" % self.fid, src.read_text())
        env = dict(os.environ, PYTHONPATH=str(REPO))
        on = subprocess.run([sys.executable, "app.py"], capture_output=True, text=True, env=env)
        self.assertEqual(on.stdout, clean)
        self.assertNotIn("not loaded", on.stderr)
        with quiet():
            r = disable(self.fid, quiet=True)
        self.assertTrue(r["restored"])
        self.assertEqual(hashlib.sha256(src.read_bytes()).hexdigest(), before)
        self.assertFalse((self.p.dir / "_ras_hardened").exists())

    def test_rejects_fake_fallback_line_without_a_boundary_failure(self):
        v = self.p.use(self.fid, "weather_fake_fallback.py")
        self.assertTrue(v["gates"]["happy"])
        self.assertFalse(v["passed"])
        self.assertFalse(v["gates"]["no_wrong"])
        bad = v["chaos"]["first_failure"]["hardened"]
        self.assertEqual((bad["class"], bad["attempts"], bad["logs"]), ("wrong", [], 1))
        self.assertIn("no dependency request failed", bad["note"])

    def test_rejects_fallback_when_the_retry_had_the_answer(self):
        v = self.p.use(self.fid, "weather_discard_answer.py")
        self.assertFalse(v["gates"]["no_wrong"])
        self.assertIn("answered in the end", v["chaos"]["first_failure"]["hardened"]["note"])

    def test_rejects_fallback_on_the_happy_path(self):
        v = self.p.use(self.fid, "weather_fallback_always.py")
        self.assertFalse(v["passed"])
        self.assertFalse(v["gates"]["happy"])
        self.assertFalse(v["gates"]["no_wrong"])

    def test_rejects_hardened_code_that_reaches_into_the_harness(self):
        hp = self.p.dir / ".ras" / "hardens" / self.fid / "hardened.py"
        for line in ("from ras_anything.agents.python import boundary", "import ras_anything.agents.python.agent", "from ras_anything.faults import Plan",
                     "import sys; B = sys.modules.get('ras_anything.agents.python.boundary')"):
            hp.write_text(line + "\n" + (FIX / "weather_good.py").read_text())
            v = verify(self.fid, quiet=True)
            self.assertFalse(v["passed"])
            self.assertIn("ras_anything.runtime only", v["fatal"]["message"])

    def test_enable_refuses_unverified_change(self):
        from ras_anything.paths import UserError

        self.p.use(self.fid, "weather_good.py")
        hp = self.p.dir / ".ras" / "hardens" / self.fid / "hardened.py"
        hp.write_text(hp.read_text() + "\n# edited after verify\n")
        with self.assertRaises(UserError):
            enable(self.fid, quiet=True)


class ShopTest(unittest.TestCase):
    fid = "orders.place_order"

    @classmethod
    def setUpClass(cls):
        cls.p = Project(FIX / "shop")
        with quiet():
            scan([sys.executable, "app.py"], seeds=1, quiet=True)
            harden(cls.fid, quiet=True)

    @classmethod
    def tearDownClass(cls):
        cls.p.close()

    def test_rejects_retrying_post_without_idempotency_key(self):
        v = self.p.use(self.fid, "shop_retry_no_key.py")
        self.assertFalse(v["passed"])
        self.assertFalse(v["gates"]["dups"])
        self.assertGreater(v["hardened"]["dups"], v["original"]["dups"])

    def test_accepts_retry_with_one_idempotency_key(self):
        v = self.p.use(self.fid, "shop_retry_with_key.py")
        self.assertTrue(v["passed"], v["gates"])
        self.assertEqual(v["hardened"]["dups"], 0)


class GetThenPostTest(unittest.TestCase):
    fid = "cart.checkout"

    @classmethod
    def setUpClass(cls):
        cls.p = Project(FIX / "cart")
        with quiet():
            scan([sys.executable, "app.py"], seeds=1, quiet=True)
            harden(cls.fid, quiet=True)

    @classmethod
    def tearDownClass(cls):
        cls.p.close()

    def test_rejects_retrying_the_whole_sequence_without_a_key(self):
        v = self.p.use(self.fid, "cart_retry_post.py")
        self.assertTrue(v["gates"]["happy"])
        self.assertFalse(v["passed"])
        self.assertFalse(v["gates"]["dups"])
        self.assertEqual(v["original"]["dups"], 0)
        self.assertGreater(v["hardened"]["dups"], 0)
        self.assertIn("timeout (per-key)", v["chaos"]["by_scenario"])  # each request is also faulted on its own
        posts = [a for a in v["chaos"]["first_failure"]["hardened"]["attempts"] if a["method"] == "POST" and a["reached"]]
        self.assertGreaterEqual(len(posts), 2)
        self.assertEqual({a["idem"] for a in posts}, {None})

    def test_accepts_step_retries_with_one_key(self):
        v = self.p.use(self.fid, "cart_retry_with_key.py")
        self.assertTrue(v["passed"], v["gates"])
        self.assertEqual(v["hardened"]["dups"], 0)


class AgentHardenedExamplesTest(unittest.TestCase):
    """Positive control: the hardened code a real agent wrote for the examples still passes verify."""

    def test_examples(self):
        found = 0
        for ex in ("weather", "payments"):
            base = REPO / "examples" / ex / ".ras" / "hardens"
            if not base.is_dir():
                continue
            p = Project(REPO / "examples" / ex)
            try:
                shutil.copytree(REPO / "examples" / ex / ".ras", p.dir / ".ras")
                for d in sorted(base.iterdir()):
                    if not (d / "calls.json").exists():
                        continue
                    found += 1
                    with quiet():
                        v = verify(d.name, quiet=True)
                    self.assertTrue(v["passed"], "%s: %s" % (d.name, v.get("gates") or v.get("fatal")))
            finally:
                p.close()
        if not found:
            self.skipTest("no agent-hardened examples recorded yet")


class SecurityAndSafetyTest(unittest.TestCase):
    def test_rejects_unlogged_self_declared_fallback(self):
        p = Project(REPO / "examples" / "weather")
        try:
            with quiet():
                scan([sys.executable, "app.py"], seeds=1, quiet=True)
                harden("weather.fetch_forecast", quiet=True)
            hp = p.dir / ".ras" / "hardens" / "weather.fetch_forecast" / "hardened.py"
            hp.write_text((FIX / "weather_swallow_none.py").read_text() + "\nRAS_FALLBACK = lambda *a, **k: None\n")
            with quiet():
                v = verify("weather.fetch_forecast", quiet=True)
            self.assertFalse(v["passed"])
            self.assertFalse(v["gates"]["no_wrong"])
        finally:
            p.close()

    def test_copied_example_enable_does_not_modify_repo(self):
        repo_weather = REPO / "examples" / "weather" / "weather.py"
        before_repo = hashlib.sha256(repo_weather.read_bytes()).hexdigest()
        p = Project(REPO / "examples" / "weather")
        try:
            shutil.copytree(REPO / "examples" / "weather" / ".ras", p.dir / ".ras")
            with quiet():
                enable("weather.fetch_forecast", quiet=True)
                self.assertEqual(hashlib.sha256(repo_weather.read_bytes()).hexdigest(), before_repo)
                r = disable("weather.fetch_forecast", quiet=True)
            self.assertTrue(r["restored"])
        finally:
            p.close()

    def test_enable_refuses_escaping_meta_path(self):
        import json
        from ras_anything.paths import UserError
        p = Project(REPO / "examples" / "weather")
        try:
            shutil.copytree(REPO / "examples" / "weather" / ".ras", p.dir / ".ras")
            mp = p.dir / ".ras" / "hardens" / "weather.fetch_forecast" / "meta.json"
            m = json.loads(mp.read_text())
            m["file"] = str(REPO / "examples" / "weather" / "weather.py")
            mp.write_text(json.dumps(m))
            with self.assertRaises(UserError):
                enable("weather.fetch_forecast", quiet=True)
        finally:
            p.close()

    def test_stacked_enable_disable_crlf_preserves_bytes(self):
        p = Project(REPO / "examples" / "payments")
        try:
            billing = p.dir / "billing.py"
            raw = billing.read_bytes().replace(b"\n", b"\r\n")
            billing.write_bytes(raw)
            before = hashlib.sha256(raw).hexdigest()
            shutil.copytree(REPO / "examples" / "payments" / ".ras", p.dir / ".ras")
            with quiet():
                verify("billing.charge", quiet=True)
                verify("billing.render_receipt", quiet=True)
                enable("billing.charge", quiet=True)
                enable("billing.render_receipt", quiet=True)
                r1 = disable("billing.charge", quiet=True)
                r2 = disable("billing.render_receipt", quiet=True)
            self.assertTrue(r1["restored"])
            self.assertTrue(r2["restored"])
            self.assertEqual(hashlib.sha256(billing.read_bytes()).hexdigest(), before)
        finally:
            p.close()


class McpTest(unittest.TestCase):
    def test_initialize_and_list(self):
        from ras_anything import mcp

        r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "ras-anything")
        r_bad = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}})
        self.assertEqual(r_bad["result"]["protocolVersion"], mcp.PROTOCOL)
        tools = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"ras_scan", "ras_harden", "ras_verify", "ras_enable",
                                                       "ras_disable", "ras_bench", "ras_report", "ras_status", "ras_flag"})
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))


if __name__ == "__main__":
    unittest.main()
