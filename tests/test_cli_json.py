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

    def test_export_json_is_the_bundle_not_a_wrapper(self):
        r = _run(["--json", "export", "--format", "json"], self.proj)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["kind"], "bundle")
        self.assertIn("snapshots", doc)

    def test_export_prom_with_json_is_wrapped(self):
        doc, _ = self._doc(["export", "--format", "prom", "--json"])
        self.assertEqual(doc["command"], "export")
        self.assertEqual(doc["result"]["format"], "prom")
        self.assertIn("ras_availability_ratio", doc["result"]["body"])

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
