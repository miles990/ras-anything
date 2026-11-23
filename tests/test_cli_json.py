"""Every result-producing command accepts --json: stdout is only JSON (wow INTEROP §1)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEATHER = REPO / "examples" / "weather"


def _run(args, cwd):
    env = dict(os.environ, PYTHONPATH=str(REPO), NO_COLOR="1")
    return subprocess.run([sys.executable, "-m", "ras_anything", *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True)


class CliJsonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.proj = Path(cls._tmp.name) / "weather"
        shutil.copytree(WEATHER, cls.proj)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _doc(self, args, cwd=None, code=0):
        cwd = cwd or self.proj
        r = _run(args, cwd)
        self.assertEqual(r.returncode, code, r.stderr)
        self.assertTrue(r.stdout.strip(), "expected JSON on stdout; stderr was:\n" + r.stderr)
        doc = json.loads(r.stdout)
        # stdout is a single JSON object (no leading log line)
        self.assertEqual(json.dumps(json.loads(r.stdout), sort_keys=True),
                         json.dumps(doc, sort_keys=True))
        return doc, r

    def test_status_json_before_or_after_the_subcommand(self):
        for args in (["--json", "status"], ["status", "--json"]):
            doc, _ = self._doc(args)
            self.assertEqual(doc["kind"], "cli-result")
            self.assertEqual(doc["command"], "status")
            self.assertTrue(doc["ok"])
            self.assertEqual(doc["exit_code"], 0)
            self.assertIn("schema_version", doc)
            self.assertIn("result", doc)
            self.assertIn("enabled", doc["result"])

    def test_doctor_json(self):
        doc, _ = self._doc(["--json", "doctor"])
        self.assertEqual(doc["command"], "doctor")
        self.assertIn("checks", doc["result"])
        self.assertGreater(len(doc["result"]["checks"]), 0)

    def test_export_formats_are_wrapped_in_one_cli_result(self):
        for fmt in ("json", "ndjson", "prom", "otlp-json", "cloudevents"):
            doc, _ = self._doc(["--json", "export", "--format", fmt])
            self.assertEqual(doc["kind"], "cli-result", fmt)
            self.assertEqual(doc["command"], "export")
            self.assertEqual(doc["result"]["format"], fmt)
            self.assertIsInstance(doc["result"]["body"], str)
            if fmt == "json":
                bundle = json.loads(doc["result"]["body"])
                self.assertEqual(bundle["kind"], "bundle")
                self.assertIn("snapshots", bundle)
            elif fmt == "prom":
                self.assertIn("ras_availability_ratio", doc["result"]["body"])
            elif fmt == "otlp-json":
                self.assertIn("resourceLogs", json.loads(doc["result"]["body"]))
            else:
                # ndjson and cloudevents are one JSON object per line
                for ln in doc["result"]["body"].splitlines():
                    if ln.strip():
                        json.loads(ln)

    def test_export_ndjson_empty_stream_is_one_cli_result(self):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td)
            (proj / ".ras").mkdir()
            # no events.ndjson: body is an empty stream, still one cli-result
            doc, _ = self._doc(["--json", "export", "--format", "ndjson"], cwd=proj)
            self.assertEqual(doc["kind"], "cli-result")
            self.assertEqual(doc["result"]["format"], "ndjson")
            self.assertEqual(doc["result"]["body"].strip(), "")

    def test_user_error_is_problem_json_on_stderr(self):
        r = _run(["--json", "scan"], self.proj)  # no app command
        self.assertEqual(r.returncode, 1)
        self.assertFalse(r.stdout.strip())
        problem = json.loads(r.stderr.strip().splitlines()[-1])
        self.assertIn("urn:ras-anything:problem:", problem["type"])
        self.assertEqual(problem["title"], problem.get("title"))
        self.assertTrue(problem["title"])

    def test_flag_list_json_after_subcommand(self):
        doc, _ = self._doc(["flag", "list", "--json"])
        self.assertEqual(doc["command"], "flag")
        self.assertEqual(doc["exit_code"], 0)
        self.assertEqual(doc["result"]["action"], "list")
        self.assertIn("flags", doc["result"])

    def test_flag_set_and_get_json_include_the_value(self):
        set_doc, _ = self._doc(["--json", "flag", "set", "ras.pattern.retry", "off"])
        self.assertEqual(set_doc["result"]["action"], "set")
        self.assertEqual(set_doc["result"]["key"], "ras.pattern.retry")
        self.assertFalse(set_doc["result"]["value"])
        get_doc, _ = self._doc(["flag", "get", "ras.pattern.retry", "--json"])
        self.assertEqual(get_doc["result"]["action"], "get")
        self.assertEqual(get_doc["result"]["key"], "ras.pattern.retry")
        self.assertFalse(get_doc["result"]["value"])
        self.assertIn("flags", set_doc["result"])

    def test_json_error_after_progress_is_only_the_problem_on_stderr(self):
        # scan prints a headline, then raises when the clean run fails
        r = _run(["--json", "scan", "--", sys.executable, "-c", "raise SystemExit(1)"], self.proj)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(r.stdout.strip())
        problem = json.loads(r.stderr.strip())  # the whole stream, not just the last line
        self.assertIn("urn:ras-anything:problem:", problem["type"])
        self.assertTrue(problem["title"])
