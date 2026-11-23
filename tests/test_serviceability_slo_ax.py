"""Unit + integration tests for V-8 serviceability (diagnose, status, RUNBOOK, DORA restore),
V-9 SLI/SLO & error-budget burn, and V-18 AX (RFC 9457 policy agent + leak scanner)."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from ras_anything import ax, diagnose, report, slo
from ras_anything.doctor import status
from ras_anything.enable import enable
from ras_anything.harden import harden
from ras_anything.scan import scan
from ras_anything.verify import verify
from tests.test_pipeline import FIX, REPO, Project, quiet


class DiagnoseAndRestoreTest(unittest.TestCase):
    def test_diagnose_otel_json_log_and_score(self):
        t0_ns = 1795423200000000000
        logs = "\n".join([
            json.dumps({
                "Timestamp": str(t0_ns),
                "SeverityText": "INFO",
                "EventName": "ras.call.start",
                "Body": "start",
                "TraceId": "a" * 32,
                "Attributes": {},
            }),
            json.dumps({
                "Timestamp": str(t0_ns + 42_500_000),
                "SeverityText": "ERROR",
                "EventName": "ras.dependency.gave_up",
                "Body": "upstream timed out",
                "TraceId": "a" * 32,
                "Attributes": {
                    "ras.dependency": "api.weather.local",
                    "ras.fault": "timeout",
                },
            }),
        ])
        d = diagnose.diagnose(logs, start_ns=t0_ns)
        self.assertTrue(d["detected"])
        self.assertGreaterEqual(d["otel_records"], 1)
        self.assertEqual(d["dependency"], "api.weather.local")
        self.assertEqual(d["fault_class"], "timeout")
        self.assertEqual(d["lines_read"], 2)
        self.assertEqual(d["ttd_ms"], 42.5)
        self.assertEqual(d["trace_id"], "a" * 32)
        sc = diagnose.score_diagnosis(d, "GET http://api.weather.local/forecast", "timeout")
        self.assertTrue(sc["accurate"])
        self.assertTrue(sc["correct_dependency"])
        self.assertTrue(sc["correct_fault"])

    def test_dora_bad_enable_restore_fixture(self):
        p = Project(REPO / "examples" / "weather")
        try:
            fid = "weather.fetch_forecast"
            with quiet():
                scan([sys.executable, "app.py"], seeds=1, quiet=True)
                harden(fid, quiet=True)
                shutil.copyfile(FIX / "weather_good.py", p.dir / ".ras" / "hardens" / fid / "hardened.py")
                v = verify(fid, max_chaos=5, quiet=True)
                self.assertTrue(v["passed"])
            # Break hardened.py after verify to simulate a bad enable (or enable then corrupt)
            with quiet():
                enable(fid, quiet=True)
            hp = p.dir / ".ras" / "hardens" / fid / "hardened.py"
            hp.write_text("raise RuntimeError('bad deploy boom')\n", encoding="utf-8")
            (p.dir / "weather.py").write_text(
                (p.dir / "weather.py").read_text(encoding="utf-8") + "\n# bad deploy\n", encoding="utf-8") if False else None
            # Also write broken block directly into enabled _ras_hardened copy
            for rhp in (p.dir / "_ras_hardened").glob("weather*.py"):
                rhp.write_text("def fetch_forecast(city):\n    raise RuntimeError('bad deploy boom')\n", encoding="utf-8")
            m = diagnose.measure_restore(p.dir, fid, [sys.executable, "app.py"])
            self.assertTrue(m["detected_bad_enable"])
            self.assertTrue(m["restored"])
            self.assertTrue(m["ttd_s"] >= 0.0)
            self.assertTrue(m["restore_s"] >= 0.0)
            self.assertTrue(m["mttr_s"] >= m["ttd_s"])
            self.assertNotIn("bad deploy boom", m["post_stderr"])
        finally:
            p.close()


class SloTest(unittest.TestCase):
    def test_parse_and_evaluate_slo_met_and_missed(self):
        tmp = Path(tempfile.mkdtemp(prefix="ras-slo-"))
        try:
            (tmp / ".ras").mkdir()
            t = slo.parse_slo("availability=99.0,p99=1s,error_rate=1%", tmp)
            self.assertAlmostEqual(t["availability"], 0.99)
            self.assertAlmostEqual(t["p99_s"], 1.0)
            self.assertAlmostEqual(t["error_rate"], 0.01)

            fake_bench = {
                "summary": {
                    "original": {"availability": 0.65, "p99_wall": 2.5},
                    "hardened": {"availability": 1.0, "p99_wall": 0.4},
                }
            }
            res = slo.evaluate(project=tmp, bench=fake_bench, targets="availability=99.0,p99=1s,error_rate=1%", save=True)
            self.assertEqual(res["original"]["status"], "missed")
            self.assertEqual(res["hardened"]["status"], "met")
            self.assertEqual(res["hardened"]["error_budget_burn_pct"], 0.0)
            self.assertGreater(res["original"]["error_budget_burn_pct"], 100.0)
            self.assertTrue((tmp / ".ras" / "slo.json").is_file())

            # Re-evaluating without slo_spec preserves user targets from .ras/slo.json
            res2 = slo.evaluate(project=tmp, bench=fake_bench, save=False)
            self.assertAlmostEqual(res2["targets"]["availability"], 0.99)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class AxTest(unittest.TestCase):
    def test_leak_scanner_catches_pii_and_accepts_clean(self):
        bad = {"detail": "user alex@example.com failed with Bearer sk-secret1234567890abcd in /Users/alex/app.py"}
        leaks = ax.scan_leaks(bad)
        self.assertIn("email", leaks)
        self.assertIn("token", leaks)
        self.assertIn("path", leaks)

        clean = {
            "type": "urn:ras-anything:problem:rate_limited",
            "title": "Rate Limited",
            "status": 429,
            "detail": "Upstream rate limit exceeded for [email]",
            "retryable": True,
            "retry_after_s": 5.0,
            "next_action": "retry_after",
            "fault_class": "rate_limited",
        }
        self.assertEqual(ax.scan_leaks(clean), [])

    def test_catalog_benchmark_improves_accuracy_and_has_zero_leaks(self):
        cat = ax.catalog_benchmark()
        self.assertEqual(cat["cases"], 10)
        self.assertEqual(cat["hardened"]["accuracy"], 1.0)
        self.assertGreater(cat["hardened"]["accuracy"], cat["original"]["accuracy"])
        self.assertEqual(cat["hardened"]["leaks"], 0)


class ReportAndStatusIntegrationTest(unittest.TestCase):
    def test_report_generates_slo_runbook_and_status_fields(self):
        p = Project(REPO / "examples" / "weather")
        try:
            fid = "weather.fetch_forecast"
            with quiet():
                scan([sys.executable, "app.py"], seeds=1, quiet=True)
                harden(fid, quiet=True)
                shutil.copyfile(FIX / "weather_good.py", p.dir / ".ras" / "hardens" / fid / "hardened.py")
                v = verify(fid, max_chaos=5, quiet=True)
                self.assertTrue(v["passed"])
                self.assertIn("serviceability", v)
                self.assertIn("ax", v)
                self.assertEqual(v["ax"]["leaks"], [])
                out = report.report("ShopTest", slo_spec="availability=95%,p99=5s,error_rate=5%", quiet=True)
            self.assertTrue(Path(out["runbook"]).is_file())
            self.assertTrue((p.dir / ".ras" / "hardens" / fid / "RUNBOOK.md").is_file())
            self.assertTrue(Path(out["slo"]).is_file())
            md_txt = Path(out["markdown"]).read_text(encoding="utf-8")
            self.assertIn("SLI / SLO", md_txt)
            self.assertIn("Serviceability", md_txt)
            self.assertIn("Agent experience", md_txt)
            self.assertIn("unavailable", md_txt)
            svg_txt = Path(out["svg"]).read_text(encoding="utf-8")
            self.assertIn("SLO (", svg_txt)

            # Write a health snapshot and verify status() picks up health + slo
            (p.dir / ".ras" / "health.json").write_text(json.dumps({
                "status": "ok", "updated": "2026-11-23T08:00:00Z", "dependencies": {"inventory": {"state": "closed"}},
            }), encoding="utf-8")
            with quiet():
                st = status()
            self.assertEqual(st["health"]["status"], "ok")
            self.assertIn("targets", st["slo"])
        finally:
            p.close()


if __name__ == "__main__":
    unittest.main()
