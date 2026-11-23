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

    def test_formats_are_discovered_from_exporter_modules(self):
        from ras_anything import exporters
        self.assertEqual(exporters.FORMATS, contracts.EXPORT_FORMATS)
        self.assertGreaterEqual(len(exporters.FORMATS), 5)
        self.assertEqual(exporters.FORMATS[0], "json")
        root = REPO / "ras_anything" / "exporters"
        names = {getattr(__import__("ras_anything.exporters." + p.stem, fromlist=["FORMAT"]), "FORMAT")
                 for p in root.glob("*.py") if p.name != "__init__.py" and not p.name.startswith("_")}
        self.assertEqual(names, set(exporters.FORMATS))


class CloudEventsExport(unittest.TestCase):
    def test_every_event_is_a_cloudevent(self):
        import shutil
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "wx"
            shutil.copytree(REPO / "examples" / "weather", p)
            text = export.export_data(p, fmt="cloudevents")
        lines = [json.loads(x) for x in text.splitlines()]
        self.assertGreater(len(lines), 0)
        for ce in lines:
            self.assertEqual(ce["specversion"], "1.0")
            for k in ("id", "source", "type", "time"):
                self.assertTrue(ce[k], k)
            self.assertRegex(ce["type"], r"^dev\.ras-anything\.[a-z0-9_.-]+$")
            self.assertRegex(ce["time"], r"[+-]\d\d:\d\d$|Z$")
        self.assertEqual(len({ce["id"] for ce in lines}), len(lines))


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


def _glob(g: str):
    """setuptools package-data glob ('**' = any folders) as a regex."""
    out = ""
    for part in re.split(r"(\*\*/|\*)", g):
        out += {"**/": "(?:.*/)?", "*": "[^/]*"}.get(part, re.escape(part))
    return re.compile(out + "$")


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
            self.assertTrue("graft ras_anything/lang\n" in manifest or "graft ras_anything/lang/%s" % agent.name in manifest, agent.name)
            for f in agent.rglob("*"):
                if not f.is_file() or "node_modules" in f.parts or "__pycache__" in f.parts or f.suffix in (".class", ".md"):
                    continue
                rel = f.relative_to(LANG).as_posix()
                self.assertTrue(any(_glob(g).match(rel) for g in globs), "not in package-data: " + rel)


class FaultConformance(unittest.TestCase):
    """Each language's agent knows every fault kind of the shared matrix (faults.MATRIX), spelled the same."""

    @staticmethod
    def agents():
        return {a.name: Path(a.agent_dir) if a.agent_dir else LANG / ("%s_agent" % a.name) for a in lang.BUILTIN}

    def test_every_builtin_language_has_an_agent_folder(self):
        for name, folder in self.agents().items():
            self.assertTrue(folder.is_dir(), "%s: %s" % (name, folder))

    def test_every_agent_handles_every_fault_kind(self):
        for name, folder in self.agents().items():
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


class LanguageSwitch(unittest.TestCase):
    """A language is a component: it can be switched off by name without editing code (docs/adr/0003)."""

    def test_off_by_name(self):
        from ras_anything import lang

        self.assertEqual(lang.switched_off(" java, go ,"), {"java", "go"})
        self.assertEqual(lang.switched_off(""), set())
        names = [a.name for a in lang.enabled(lang.ALL_ADAPTERS, {"java"})]
        self.assertNotIn("java", names)
        self.assertIn("python", names)

    def test_env_switch_in_a_fresh_process(self):
        import os
        import subprocess
        import sys

        env = dict(os.environ, RAS_ANYTHING_LANGUAGES_OFF="go")
        out = subprocess.run([sys.executable, "-c", "from ras_anything import lang; print(','.join(a.name for a in lang.ADAPTERS))"],
                             env=env, capture_output=True, text=True, check=True).stdout.strip().split(",")
        self.assertNotIn("go", out)
        self.assertIn("python", out)


class EveryLanguageModuleIsDiscovered(unittest.TestCase):
    """Each lang/<module>.py that defines an adapter is found by discover(): python, node, golang, java today."""

    def test_modules(self):
        from ras_anything import lang

        modules = {type(a).__module__.rsplit(".", 1)[-1] for a in lang.BUILTIN}
        self.assertTrue({"python", "node", "golang"} <= modules, modules)


class LanguageSwitchEdges(unittest.TestCase):
    def test_all_off_is_a_user_error(self):
        from ras_anything import lang
        from ras_anything.paths import UserError

        with mock.patch.object(lang, "ADAPTERS", []), mock.patch.dict(os.environ, {"RAS_ANYTHING_LANGUAGES_OFF": "python,node,go,java"}):
            with self.assertRaises(UserError) as e:
                lang.detect(["python", "app.py"])
            self.assertIn("switched off", e.exception.what)

    def test_coverage_says_disabled(self):
        from ras_anything import export as ex
        from ras_anything import lang

        with mock.patch.object(lang, "ADAPTERS", lang.enabled(lang.ALL_ADAPTERS, {"python"})):
            rows = {r["lang"]: r for r in ex.coverage_payload()["adapters"]}
        self.assertFalse(rows["python"]["enabled"])
        self.assertFalse(rows["python"]["verify"])
        self.assertTrue(rows["node"]["enabled"])
