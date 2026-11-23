"""Tests for Stage V (V-19): JSON Schemas, .ras/out emission, CLI export, MCP tools, and dashboard-sample."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ras_anything import export, mcp

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "docs" / "schema"
SAMPLE_DIR = ROOT / "examples" / "dashboard-sample"
SAMPLE_OUT = SAMPLE_DIR / "out"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class SchemaExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.run_schema = _load(SCHEMA_DIR / "ras-run.schema.json")
        cls.event_schema = _load(SCHEMA_DIR / "ras-event.schema.json")

    def test_schemas_are_valid_draft_2020_12_definitions(self):
        self.assertEqual(self.run_schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(self.event_schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertIn("schema_version", self.run_schema["required"])
        self.assertIn("kind", self.run_schema["required"])
        self.assertIn("seq", self.event_schema["required"])
        self.assertIn("event_type", self.event_schema["required"])

    def test_validator_rejects_malformed_run_and_event(self):
        with self.assertRaises(ValueError):
            export.validate_json_schema({"kind": "scan"}, self.run_schema)  # missing schema_version/generated_at
        with self.assertRaises(ValueError):
            export.validate_json_schema(
                {"schema_version": "2.0.0", "kind": "scan", "generated_at": "2026-11-23T00:00:00Z"},
                self.run_schema,
            )
        with self.assertRaises(ValueError):
            export.validate_json_schema(
                {"schema_version": "1.0.0", "kind": "unknown_kind", "generated_at": "2026-11-23T00:00:00Z"},
                self.run_schema,
            )
        with self.assertRaises(ValueError):
            export.validate_json_schema(
                {"schema_version": "1.0.0", "seq": 0, "ts": "2026-11-23T00:00:00Z", "event_type": "x", "severity": "DEBUG"},
                self.event_schema,
            )

    def test_dashboard_sample_all_files_validate_against_schemas_and_have_no_path_leaks(self):
        self.assertTrue(SAMPLE_OUT.is_dir())
        bundle = _load(SAMPLE_OUT / "bundle.json")
        export.validate_json_schema(bundle, self.run_schema)
        self.assertEqual(bundle["kind"], "bundle")
        self.assertGreaterEqual(len(bundle["projects"]), 5)

        patterns = _load(SAMPLE_OUT / "patterns.json")
        export.validate_json_schema(patterns, self.run_schema)
        self.assertEqual(patterns["kind"], "patterns")
        self.assertEqual(len(patterns["patterns"]), 13)

        checked_files = 0
        for p in sorted(SAMPLE_OUT.rglob("*.json")):
            doc = _load(p)
            if p.name == "otlp.json":
                self.assertIn("resourceMetrics", doc)
            else:
                export.validate_json_schema(doc, self.run_schema)
            raw = p.read_text(encoding="utf-8")
            self.assertNotIn("/workspace/", raw, f"path leak in {p}")
            self.assertNotIn("/home/", raw, f"path leak in {p}")
            checked_files += 1
        self.assertGreaterEqual(checked_files, 35)

        ev_count = 0
        for p in sorted(SAMPLE_OUT.rglob("events.ndjson")):
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                ev = json.loads(line)
                export.validate_json_schema(ev, self.event_schema)
                self.assertNotIn("/workspace/", line)
                ev_count += 1
        self.assertGreaterEqual(ev_count, 20)

    def test_dashboard_sample_html_embeds_real_bundle_and_screenshot_exists(self):
        html_path = SAMPLE_DIR / "index.html"
        self.assertTrue(html_path.is_file())
        html = html_path.read_text(encoding="utf-8")
        m = re.search(r'<script id="embedded-bundle" type="application/json">(.*?)</script>', html, re.S)
        self.assertIsNotNone(m)
        embedded = json.loads(m.group(1))
        export.validate_json_schema(embedded, self.run_schema)
        self.assertEqual(set(embedded["projects"].keys()), set(_load(SAMPLE_OUT / "bundle.json")["projects"].keys()))

        png = ROOT / "docs" / "cards" / "dashboard-sample.png"
        self.assertTrue(png.is_file())
        data = png.read_bytes()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(data), 50_000)

    def test_emit_sync_cli_export_and_mcp_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td)
            # Seed a synthetic minimal .ras workspace copied from support-ai without touching repo
            shutil.copytree(ROOT / "examples" / "support-ai" / ".ras", proj / ".ras")
            shutil.rmtree(proj / ".ras" / "out", ignore_errors=True)

            out = export.sync_out(proj)
            self.assertIn("report", out)
            self.assertIn("coverage", out)
            self.assertTrue((proj / ".ras" / "out" / "report.json").is_file())
            self.assertTrue((proj / ".ras" / "out" / "slo.json").is_file())

            # Emit events and test cursor pagination via read_events and MCP ras_events
            e1 = export.emit_event(proj, "verify.passed", function_id="support.triage_ticket", data={"avail": 1.0})
            e2 = export.emit_event(proj, "bench.completed", severity="WARN", data={"availability_hardened": 1.0})
            self.assertGreater(e2["seq"], e1["seq"])

            page1 = export.read_events(proj, since=0, limit=1)
            self.assertEqual(len(page1["events"]), 1)
            page2 = export.read_events(proj, since=page1["cursor"], limit=10)
            self.assertGreaterEqual(len(page2["events"]), 1)
            self.assertTrue(all(ev["seq"] > page1["cursor"] for ev in page2["events"]))

            # CLI export across all 4 formats
            env = dict(os.environ, PYTHONPATH=str(ROOT))
            for fmt in ("json", "ndjson", "prom", "otlp-json"):
                r = subprocess.run(
                    [sys.executable, "-m", "ras_anything", "export", "--format", fmt],
                    cwd=str(proj), env=env, capture_output=True, text=True, check=True,
                )
                if fmt == "json":
                    parsed = json.loads(r.stdout)
                    export.validate_json_schema(parsed, self.run_schema)
                elif fmt == "ndjson":
                    lines = [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
                    self.assertGreaterEqual(len(lines), 2)
                    for ev in lines:
                        export.validate_json_schema(ev, self.event_schema)
                elif fmt == "prom":
                    self.assertIn("ras_availability_ratio", r.stdout)
                    self.assertIn("ras_ai_semantic_success_ratio", r.stdout)
                elif fmt == "otlp-json":
                    otlp = json.loads(r.stdout)
                    self.assertIn("resourceMetrics", otlp)

            # MCP ras_export & ras_events
            self.assertIn("ras_export", mcp.TOOLS)
            self.assertIn("ras_events", mcp.TOOLS)
            res_ev = mcp.call_tool("ras_events", {"cwd": str(proj), "since": e1["seq"] - 1, "limit": 5})
            self.assertFalse(res_ev["isError"])
            ev_payload = json.loads(res_ev["content"][0]["text"])
            self.assertGreaterEqual(len(ev_payload["events"]), 2)

            old_pp = os.environ.get("PYTHONPATH")
            os.environ["PYTHONPATH"] = str(ROOT)
            try:
                res_exp = mcp.call_tool("ras_export", {"cwd": str(proj), "format": "prom"})
                self.assertFalse(res_exp["isError"])
                self.assertIn("ras_availability_ratio", res_exp["content"][0]["text"])
            finally:
                if old_pp is None:
                    os.environ.pop("PYTHONPATH", None)
                else:
                    os.environ["PYTHONPATH"] = old_pp



    def test_export_refuses_symlink_escape_and_reports_corrupt_json_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "proj"
            outside = Path(td) / "outside"
            (proj / ".ras").mkdir(parents=True)
            outside.mkdir()
            sentinel = outside / "status.json"
            sentinel.write_text("DO NOT OVERWRITE\n", encoding="utf-8")
            (proj / ".ras" / "out").symlink_to(outside)

            env = dict(os.environ, PYTHONPATH=str(ROOT))
            r = subprocess.run(
                [sys.executable, "-m", "ras_anything", "export", "--format", "json"],
                cwd=str(proj), env=env, capture_output=True, text=True,
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "DO NOT OVERWRITE\n")

            # Corrupt JSON produces UserError (exit 1, no Traceback)
            (proj / ".ras" / "out").unlink()
            (proj / ".ras" / "scan.json").write_text("{", encoding="utf-8")
            r2 = subprocess.run(
                [sys.executable, "-m", "ras_anything", "export", "--format", "json"],
                cwd=str(proj), env=env, capture_output=True, text=True,
            )
            self.assertEqual(r2.returncode, 1)
            self.assertNotIn("Traceback", r2.stderr)
            self.assertIn("corrupt JSON", r2.stdout + r2.stderr)

if __name__ == "__main__":
    unittest.main()
