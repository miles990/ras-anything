"""Language adapters: detection, launchers, and that the core stays language-neutral."""

import ast
import inspect
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ras_anything import lang
from ras_anything.cli import main
from ras_anything.lang.base import LangAdapter
from ras_anything.paths import UserError
from tests.test_pipeline import FIX, REPO, Project, harden, quiet, scan, verify

PKG = REPO / "ras_anything"


class DetectTest(unittest.TestCase):
    def test_python_launchers(self):
        for cmd in (["python", "a.py"], ["python3.12", "a.py"], ["/x/.venv/bin/python3", "a.py"], ["pypy3", "a.py"], ["py", "a.py"],
                    ["python3.13t", "a.py"], ["C:\\Py\\python.exe", "a.py"], ["uv", "run", "a.py"], ["poetry", "run", "python", "a.py"],
                    ["pipenv", "run", "a.py"], ["pdm", "run", "a.py"], ["hatch", "run", "a.py"], ["rye", "run", "a.py"]):
            self.assertEqual(lang.detect(cmd).name, "python", cmd)

    def test_unknown_programs_list_what_is_supported(self):
        for cmd in (["./a.out"], ["ruby", "a.rb"], ["pythonista", "x"], ["uv", "pip", "list"], ["npx", "tsx", "a.ts"]):
            with self.assertRaises(UserError) as cm:
                lang.detect(cmd)
            self.assertIn("python", cm.exception.impact)
            self.assertIn("uv", cm.exception.impact)
            self.assertIn("node", cm.exception.impact)
            self.assertTrue(cm.exception.nxt.startswith("scan -- "))

    def test_hints(self):
        def hint(*cmd):
            with self.assertRaises(UserError) as cm:
                lang.detect(list(cmd))
            return cm.exception.impact

        for prog in ("npx", "tsx", "ts-node"):
            self.assertIn("Node 22.6+ runs TypeScript itself: ras-anything scan -- node app.ts", hint(prog, "a.ts"))
        self.assertIn("python -m pytest", hint("pytest"))
        self.assertIn("python ./app.py", hint("./app.py"))
        self.assertIn("node server.mjs", hint("server.mjs"))

    def test_cli_error_has_what_impact_next(self):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["scan", "--", "ruby", "app.rb"])
        self.assertEqual(code, 1)
        lines = err.getvalue().splitlines()
        self.assertIn("ruby", lines[0])
        self.assertIn("supported launchers", lines[1])
        self.assertIn("next: ras-anything scan -- python app.py", lines[-1])

    def test_by_name(self):
        self.assertEqual(lang.by_name("python").name, "python")
        self.assertEqual(lang.by_name(None).name, "python")  # scans made by v0.1
        self.assertRaises(UserError, lang.by_name, "cobol")


class EnvRunnerTest(unittest.TestCase):
    def test_uv_run_is_unwrapped_to_the_projects_python(self):
        a = lang.by_name("python")
        with tempfile.TemporaryDirectory() as d:
            uv = Path(d) / "uv"
            uv.write_text("#!/bin/sh\n[ \"$1\" = run ] && shift\n[ \"$1\" = python ] && shift\nexec %s \"$@\"\n" % sys.executable)
            uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
            with mock.patch.dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"]):
                self.assertEqual(a.resolve(["uv", "run", "app.py", "-x"], Path(d)), [sys.executable, "app.py", "-x"])
                self.assertEqual(a.resolve(["uv", "run", "python", "-m", "pkg"], Path(d)), [sys.executable, "-m", "pkg"])
                for bad in (["uv", "run"], ["uv", "run", "--with", "x", "app.py"], ["uv", "run", "flask"]):
                    self.assertRaises(UserError, a.resolve, bad, Path(d))
                # the whole loop starts from `uv run`, and the stored command stays as typed
                p = Project(FIX / "shop")
                try:
                    with quiet():
                        res = scan(["uv", "run", "app.py"], seeds=1, quiet=True)
                        harden("orders.place_order", quiet=True)
                        v = p.use("orders.place_order", "shop_retry_with_key.py")
                    self.assertEqual(res["cmd"], ["uv", "run", "app.py"])
                    self.assertEqual(res["lang"], "python")
                    self.assertTrue(v["passed"])
                finally:
                    p.close()

    def test_an_app_the_agent_cannot_enter_is_an_error_not_an_empty_scan(self):
        p = Project(FIX / "shop")
        try:
            with self.assertRaises(UserError) as cm, quiet():
                scan([sys.executable, "-I", "app.py"], seeds=1, quiet=True)
        finally:
            p.close()
        self.assertTrue("couldn't watch" in cm.exception.what or "clean run failed" in cm.exception.what)


class LayeringTest(unittest.TestCase):
    """The core calls the adapter only."""

    CORE = [p for p in PKG.glob("*.py") if p.name not in ("runtime.py", "__main__.py", "__init__.py")]

    def test_core_does_not_import_a_language(self):
        for p in self.CORE:
            for n in ast.walk(ast.parse(p.read_text())):
                names = [a.name for a in n.names] if isinstance(n, (ast.Import, ast.ImportFrom)) else []
                mod = getattr(n, "module", None) or ""
                for x in names + [mod]:
                    self.assertFalse(x.startswith("agents") or x.startswith("lang.") or x in ("python_static", "boundary", "values")
                                     or (mod == "lang" and x != "lang"), "%s imports %s" % (p.name, x))
                self.assertNotEqual(names, ["ast"], p.name)

    def test_core_has_no_language_branches(self):
        bad = re.compile(r"hardened\.py|\.py[\"']|PYTHONPATH|sitecustomize|adapter\.name\s*==|lang\s*==\s*[\"']")
        for p in self.CORE:
            hits = [ln for ln in p.read_text().replace("python app.py", "").splitlines() if bad.search(ln)]
            self.assertEqual(hits, [], p.name)

    def test_interface_is_complete(self):
        abstract = LangAdapter.__abstractmethods__
        for a in lang.ADAPTERS:
            for attr in ("name", "label", "launchers", "example", "boundaries", "fence", "hardened_name"):
                self.assertTrue(getattr(a, attr), attr)
            for m in abstract:
                self.assertEqual(list(inspect.signature(getattr(LangAdapter, m)).parameters)[1:],
                                 list(inspect.signature(getattr(a, m)).parameters), "%s.%s" % (a.name, m))

    def test_architecture_document_covers_the_contract(self):
        doc = (REPO / "docs" / "ARCHITECTURE.md").read_text()
        for m in LangAdapter.__abstractmethods__:
            self.assertIn("`%s`" % m, doc)
        for word in ("RAS_ANYTHING_CFG", "ras-anything-cassette", "$bytes", "$proj", "is_fallback", "unanswered", "outage_rate"):
            self.assertIn(word, doc)


if __name__ == "__main__":
    unittest.main()
