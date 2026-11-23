"""Architecture fitness functions for ras-anything itself (docs/REVIEW-PRACTICES.md, docs/adr/0002).

They fail when an extension point drifts: an export format named in one place but not another, a language
agent that isn't packaged, a fault kind one runtime doesn't know, a plugin that can break the core.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ras_anything import contracts, export, faults, lang, mcp
from ras_anything.cli import parser
from ras_anything.lang.base import LangAdapter
from ras_anything.lang.python import PythonAdapter

REPO = Path(__file__).resolve().parent.parent
LANG = REPO / "ras_anything" / "lang"


class ExportFormatContract(unittest.TestCase):
    def test_every_format_has_one_renderer_and_the_cli_and_mcp_agree(self):
        self.assertEqual(tuple(export.RENDERERS), contracts.EXPORT_FORMATS)
        sub = next(a for a in parser()._actions if a.dest == "command")
        fmt = next(a for a in sub.choices["export"]._actions if a.dest == "format")
        self.assertEqual(tuple(fmt.choices), contracts.EXPORT_FORMATS)
        self.assertEqual(tuple(mcp.TOOLS["ras_export"][1]["format"]["enum"]), contracts.EXPORT_FORMATS)


class _Ruby(PythonAdapter):  # a stand-in plugin: any LangAdapter works
    name, label = "rubyx", "RubyX"


class _EP:
    def __init__(self, name, obj=None, err=None, value="pkg:Obj"):
        self.name, self._obj, self._err, self.value = name, obj, err, value

    def load(self):
        if self._err:
            raise self._err
        return self._obj


class LanguagePluginContract(unittest.TestCase):
    def _plugins(self, eps):
        err = io.StringIO()
        with mock.patch.object(lang.metadata, "entry_points", return_value=eps), redirect_stderr(err):
            return lang.plugins(), err.getvalue()

    def test_a_plugin_adds_a_language_without_editing_the_core(self):
        got, err = self._plugins([_EP("rubyx", _Ruby)])
        self.assertEqual([a.name for a in got], ["rubyx"])
        self.assertEqual(err, "")

    def test_broken_or_clashing_plugins_are_skipped_not_fatal(self):
        class Py(PythonAdapter):
            pass
        got, err = self._plugins([_EP("boom", err=ImportError("no module")), _EP("python", Py), _EP("notadapter", object)])
        self.assertEqual(got, [])
        self.assertIn("boom", err)
        self.assertIn("built in", err)
        self.assertIn("notadapter", err)

    def test_builtins_come_first_and_names_are_unique(self):
        names = [a.name for a in lang.ADAPTERS]
        self.assertEqual(names[:len(lang.BUILTIN)], [a.name for a in lang.BUILTIN])
        self.assertEqual(len(names), len(set(names)))
        for a in lang.ADAPTERS:
            self.assertIsInstance(a, LangAdapter)


class PackagingContract(unittest.TestCase):
    """Every agent folder ships in the wheel and the sdist (adding a language used to need 2 manual edits here)."""

    def test_every_agent_folder_is_packaged(self):
        try:
            import tomllib
        except ImportError:  # Python 3.10
            self.skipTest("tomllib needs Python 3.11+")
        cfg = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        globs = cfg["tool"]["setuptools"]["package-data"]["ras_anything.lang"]
        manifest = (REPO / "MANIFEST.in").read_text(encoding="utf-8")
        for agent in sorted(p for p in LANG.glob("*_agent") if p.is_dir()):
            self.assertIn("graft ras_anything/lang/%s" % agent.name, manifest, agent.name)
            for f in agent.rglob("*"):
                if not f.is_file() or "node_modules" in f.parts or "__pycache__" in f.parts or f.suffix in (".class", ".md"):
                    continue
                rel = f.relative_to(LANG).as_posix()
                self.assertTrue(any(Path(rel).match(g) for g in globs), "not in package-data: " + rel)


class FaultConformance(unittest.TestCase):
    """Each language's agent knows every fault kind of the shared matrix (faults.MATRIX), spelled the same."""

    AGENTS = {
        "python": REPO / "ras_anything" / "agents" / "python",
        "node": LANG / "node_agent",
        "go": LANG / "go_agent",
        "java": LANG / "java_agent",
    }

    def test_every_builtin_language_has_an_agent_folder_listed_here(self):
        self.assertEqual(sorted(self.AGENTS), sorted(a.name for a in lang.BUILTIN))

    def test_every_agent_handles_every_fault_kind(self):
        for name, folder in self.AGENTS.items():
            text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in folder.rglob("*")
                             if p.is_file() and p.suffix in (".py", ".cjs", ".go", ".java") and "node_modules" not in p.parts)
            for kind in faults.MATRIX:
                self.assertRegex(text, r"[\"']%s[\"']" % re.escape(kind), "%s agent doesn't know fault %r" % (name, kind))


class DoctorStatusRegression(unittest.TestCase):
    def test_status_reads_the_health_file_named_by_the_environment(self):
        from ras_anything import doctor
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / ".ras").mkdir()
            (p / ".ras" / "scan.json").write_text(json.dumps({"functions": [], "display_cmd": "python app.py", "created": "2026-11-23T00:00"}))
            h = p / "elsewhere-health.json"
            h.write_text(json.dumps({"status": "ok", "updated": "2026-11-23T00:00:00Z", "dependencies": {}}))
            with mock.patch.object(doctor, "find_project", return_value=p), \
                    mock.patch.dict(os.environ, {"RAS_ANYTHING_HEALTH_FILE": str(h)}), redirect_stdout(io.StringIO()):
                out = doctor.status()
        self.assertEqual(out["health"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
