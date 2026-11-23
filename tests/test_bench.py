"""bench: a run may only count as degraded when a dependency was seen to fail in it."""

import shutil
import sys
import unittest

from ras_anything import bench as bench_mod
from ras_anything.enable import enable
from ras_anything.harden import harden
from ras_anything.runner import RunResult
from ras_anything.scan import scan
from tests.test_pipeline import FIX, REPO, Project, quiet


class ClassifyTest(unittest.TestCase):
    def test_marker_or_fallback_needs_an_observed_failure(self):
        r = RunResult(0, "a unavailable\n", "", 0.1, {})
        self.assertEqual(bench_mod.classify(r, "a 20\n", ["unavailable"], 0, failures=1), "degraded")
        self.assertEqual(bench_mod.classify(r, "a 20\n", ["unavailable"], 0, failures=0), "wrong")
        self.assertEqual(bench_mod.classify(r, "a 20\n", [], 1, failures=0), "wrong")
        self.assertEqual(bench_mod.classify(r, "a 20\n", [], 1, failures=2), "degraded")


class FallbackCreditTest(unittest.TestCase):
    def test_fake_fallback_calls_are_not_credited(self):
        """Swap a lying version in by hand (verify would refuse it): bench gives it no fallback credit."""
        p = Project(REPO / "examples" / "weather")
        try:
            fid = "weather.fetch_forecast"
            with quiet():
                scan([sys.executable, "app.py"], seeds=1, quiet=True)
                harden(fid, quiet=True)
                p.use(fid, "weather_good.py")
                enable(fid, quiet=True)
                shutil.copyfile(FIX / "weather_fallback_always.py", p.dir / "_ras_hardened" / "weather__fetch_forecast.py")
                res = bench_mod.bench(seeds=2, quiet=True)
            self.assertEqual(res["functions"]["hardened"][fid]["fallback"], 0)
            self.assertEqual(res["summary"]["hardened"]["wrong"], 2)
        finally:
            p.close()


if __name__ == "__main__":
    unittest.main()
