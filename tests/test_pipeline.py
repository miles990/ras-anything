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
                    if not (d / "calls.pkl").exists():
                        continue
                    found += 1
                    with quiet():
                        v = verify(d.name, quiet=True)
                    self.assertTrue(v["passed"], "%s: %s" % (d.name, v.get("gates") or v.get("fatal")))
            finally:
                p.close()
        if not found:
            self.skipTest("no agent-hardened examples recorded yet")


class McpTest(unittest.TestCase):
    def test_initialize_and_list(self):
        from ras_anything import mcp

        r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "ras-anything")
        tools = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"ras_scan", "ras_harden", "ras_verify", "ras_enable",
                                                       "ras_disable", "ras_bench", "ras_report", "ras_status"})
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))


if __name__ == "__main__":
    unittest.main()
