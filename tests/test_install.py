"""Stage I (build2i / V-6, V-11): install.sh, make install, skills, MCP/plugin, RELEASE.md."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.sh"
MAKEFILE = REPO / "Makefile"
SKILL_SRC = REPO / "skills" / "ras-anything" / "SKILL.md"


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _path_hiding(cmds: list[str], extra_prepend: list[str] | None = None) -> str:
    hidden = set()
    for cmd in cmds:
        found = shutil.which(cmd)
        if found:
            hidden.add(str(Path(found).resolve().parent))
    parts = []
    for p in (extra_prepend or []):
        if p and p not in parts:
            parts.append(p)
    for p in os.environ.get("PATH", "").split(":"):
        if not p or p in hidden or p in parts:
            continue
        parts.append(p)
    # Always keep a working python3 and coreutils visible.
    for essential in ("python3", "sh", "grep", "cp", "mkdir"):
        found = shutil.which(essential)
        if found:
            parent = str(Path(found).resolve().parent)
            if parent not in parts:
                parts.append(parent)
    return ":".join(parts)


def _clean_env(home: Path, *, with_pipx: bool) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    env["HOME"] = str(home)
    env["PIPX_HOME"] = str(home / ".local" / "pipx")
    env["PIPX_BIN_DIR"] = str(home / ".local" / "bin")
    env["NO_COLOR"] = "1"
    env.pop("FORCE_COLOR", None)
    # Do not inherit the repo .venv or a developer ras-anything already on PATH.
    hide = ["pipx"] if not with_pipx else []
    prepend = [str(home / ".local" / "bin")]
    env["PATH"] = _path_hiding(hide, extra_prepend=prepend)
    return env


def _run(argv: list[str], *, env: dict, cwd=None, timeout=300, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=cwd or REPO, env=env, text=True, capture_output=True, timeout=timeout, check=check,
    )


def _read_skill(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.lstrip().startswith("---"):
        raise AssertionError(f"{path} is not a SKILL.md with YAML frontmatter")
    parts = text.split("---", 2)
    if "name: ras-anything" not in parts[1]:
        raise AssertionError(f"{path} frontmatter missing name: ras-anything")
    return text


def _mcp_handshake(binary: Path, env: dict) -> tuple[dict, dict]:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "test_install", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    payload = "".join(json.dumps(m) + "\n" for m in messages)
    p = subprocess.run(
        [str(binary), "mcp"], input=payload, cwd=str(REPO), env=env,
        text=True, capture_output=True, timeout=30, check=True,
    )
    lines = [json.loads(s) for s in p.stdout.splitlines() if s.strip()]
    by_id = {msg.get("id"): msg for msg in lines if "id" in msg}
    if 1 not in by_id or 2 not in by_id:
        raise AssertionError(f"MCP handshake incomplete: stdout={p.stdout!r} stderr={p.stderr!r}")
    return by_id[1], by_id[2]


class InstallAndReleaseTests(unittest.TestCase):
    def test_install_sh_is_posix_and_makefile_delegates(self):
        self.assertTrue(INSTALL_SH.is_file())
        self.assertTrue(os.access(INSTALL_SH, os.X_OK), "install.sh must be executable")
        subprocess.run(["sh", "-n", str(INSTALL_SH)], check=True, capture_output=True)
        make = MAKEFILE.read_text(encoding="utf-8")
        self.assertIn("./install.sh", make)
        self.assertIn("./install.sh --uninstall", make)

    def test_readme_is_dual_purpose_and_release_doc_is_gated(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        for needle in (
            "./install.sh", "make install", "make uninstall",
            "~/.claude/skills", "~/.codex/skills",
            "## Use it yourself (CLI)",
            "## Use it from an agent (skill / MCP / plugin)",
            "## How it works", "adapter", "wire format",
            "## Supported languages", "## Next languages",
            "ras_export", "ras_events",
            "claude mcp add ras-anything -- ras-anything mcp",
        ):
            self.assertIn(needle, readme)

        release = (REPO / "docs" / "RELEASE.md").read_text(encoding="utf-8")
        low = release.lower()
        for needle in ("alex gate", "v0.2.0", "python -m build", "twine check",
                       "trusted publishing", "do not make it public", "private"):
            self.assertIn(needle, low)
        self.assertIn("do not upload to pypi or npm", low)
        self.assertIn("never publishes to PyPI or npm", INSTALL_SH.read_text(encoding="utf-8"))
        self.assertIn("A separate npm package is **not required**", release)
        self.assertIn("there is no `./release.sh`", release)

    def test_plugin_marketplace_and_mcp_manifests_are_loadable(self):
        plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
        mcp = json.loads((REPO / ".mcp.json").read_text())
        self.assertEqual(plugin["name"], "ras-anything")
        self.assertTrue(plugin["version"])
        self.assertEqual(market["name"], "ras-anything")
        self.assertEqual(market["plugins"][0]["name"], "ras-anything")
        self.assertEqual(market["plugins"][0]["source"], "./")
        server = mcp["mcpServers"]["ras-anything"]
        self.assertEqual(server["command"], "ras-anything")
        self.assertEqual(server["args"], ["mcp"])
        self.assertTrue((REPO / "commands" / "ras.md").is_file())
        self.assertTrue(SKILL_SRC.is_file())
        _read_skill(SKILL_SRC)

    def test_clean_home_venv_install_is_idempotent_and_uninstalls(self):
        with tempfile.TemporaryDirectory(prefix="ras-install-") as td:
            home = Path(td) / "home"
            home.mkdir()
            # Leave an unrelated skill alone — uninstall must not touch it.
            other = home / ".claude" / "skills" / "unrelated" / "SKILL.md"
            other.parent.mkdir(parents=True)
            other.write_text("# keep me\n", encoding="utf-8")

            env = _clean_env(home, with_pipx=False)
            self.assertIsNone(shutil.which("pipx", path=env["PATH"]), "pipx must be hidden to exercise the venv path")

            first = _run(["./install.sh"], env=env, timeout=360)
            self._assert_install_ok(home, env, first)

            # Idempotent refresh.
            second = _run(["./install.sh"], env=env, timeout=360)
            self._assert_install_ok(home, env, second)

            if _which("make"):
                made = _run(["make", "install"], env=env, timeout=360)
                self._assert_install_ok(home, env, made)

            _run(["./install.sh", "--uninstall"], env=env, timeout=120)
            self._assert_uninstalled(home)
            self.assertTrue(other.is_file(), "unrelated skills must survive uninstall")
            # Uninstall is itself idempotent.
            again = _run(["./install.sh", "--uninstall"], env=env, timeout=60)
            self.assertEqual(again.returncode, 0)

    def test_mcp_tools_match_plugin_surface_from_clean_install(self):
        """A slightly shorter path: one venv install, live MCP handshake, then uninstall."""
        with tempfile.TemporaryDirectory(prefix="ras-mcp-") as td:
            home = Path(td) / "home"
            home.mkdir()
            env = _clean_env(home, with_pipx=False)
            _run(["./install.sh"], env=env, timeout=360)
            try:
                shim = home / ".local" / "bin" / "ras-anything"
                init, tools = _mcp_handshake(shim, env)
                info = init["result"]["serverInfo"]
                self.assertEqual(info["name"], "ras-anything")
                names = {t["name"] for t in tools["result"]["tools"]}
                self.assertEqual(names, {
                    "ras_scan", "ras_harden", "ras_verify", "ras_enable", "ras_disable",
                    "ras_bench", "ras_report", "ras_status", "ras_replay", "ras_flag",
                    "ras_export", "ras_events",
                })
            finally:
                _run(["./install.sh", "--uninstall"], env=env, timeout=120, check=False)

    @unittest.skipUnless(_which("pipx"), "pipx not installed")
    def test_pipx_install_path_under_isolated_home(self):
        with tempfile.TemporaryDirectory(prefix="ras-pipx-") as td:
            home = Path(td) / "home"
            home.mkdir()
            env = _clean_env(home, with_pipx=True)
            # pipx lives in the real user site; a fake HOME would hide it from /usr/bin/python3.
            real_userbase = Path.home() / ".local"
            if (real_userbase / "lib").is_dir():
                env["PYTHONUSERBASE"] = str(real_userbase)
            probe = subprocess.run(["pipx", "--version"], env=env, capture_output=True, text=True)
            if probe.returncode != 0:
                self.skipTest("pipx is present but not runnable in an isolated HOME")
            self.assertIsNotNone(shutil.which("pipx", path=env["PATH"]))
            _run(["./install.sh"], env=env, timeout=360)
            try:
                self._assert_install_ok(home, env, subprocess.CompletedProcess(["./install.sh"], 0, "", ""))
                marker = (home / ".local" / "share" / "ras-anything" / "installed-by-install-sh").read_text()
                self.assertTrue(marker.startswith("pipx"), marker)
            finally:
                _run(["./install.sh", "--uninstall"], env=env, timeout=120, check=False)
                self._assert_uninstalled(home)

    def _assert_install_ok(self, home: Path, env: dict, completed: subprocess.CompletedProcess) -> None:
        shim = home / ".local" / "bin" / "ras-anything"
        self.assertTrue(shim.is_file(), f"missing shim; stdout={completed.stdout}\nstderr={completed.stderr}")
        self.assertTrue(os.access(shim, os.X_OK))
        ver = _run([str(shim), "--version"], env=env, timeout=30)
        self.assertIn("0.1.0", ver.stdout)

        claude = home / ".claude" / "skills" / "ras-anything" / "SKILL.md"
        codex = home / ".codex" / "skills" / "ras-anything" / "SKILL.md"
        self.assertTrue(claude.is_file())
        self.assertTrue(codex.is_file())
        src = _read_skill(SKILL_SRC)
        self.assertEqual(_read_skill(claude), src)
        self.assertEqual(_read_skill(codex), src)

        # Bundled language agents must be present in a non-editable install.
        locate = (
            "from pathlib import Path\n"
            "import ras_anything.lang as m\n"
            "root = Path(m.__file__).resolve().parent\n"
            "print(root / 'node_agent' / 'boundary.cjs')\n"
            "print(root / 'go_agent' / 'rasagent' / 'agent.go')\n"
        )
        # Find the python that belongs to the install (venv or pipx).
        probe = _run([str(shim), "--version"], env=env, timeout=30)
        self.assertEqual(probe.returncode, 0)
        py = None
        venv_py = home / ".local" / "share" / "ras-anything" / "venv" / "bin" / "python"
        if venv_py.is_file():
            py = venv_py
        else:
            # pipx: resolve via the shim's shebang or `python -c` through ras-anything's env.
            # `ras-anything` is a console script; ask it by running python from the same argv0 dir.
            cand = shim.parent / "python"
            py = shim  # fallback: run a tiny module from the installed pkg via PYTHONPATH-free -c through python -c after locating
        if venv_py.is_file():
            got = _run([str(venv_py), "-c", locate], env=env, timeout=30)
            paths = [Path(s) for s in got.stdout.splitlines() if s.strip()]
            self.assertEqual(len(paths), 2, got.stdout)
            for path in paths:
                self.assertTrue(path.is_file(), f"packaged agent missing: {path}")

        # Skills discovery: both well-known directories yield a readable ras-anything skill.
        discovered = list((home / ".claude" / "skills").glob("*/SKILL.md")) + list(
            (home / ".codex" / "skills").glob("*/SKILL.md")
        )
        names = {_read_skill(p) and p.parent.name for p in discovered if p.parent.name == "ras-anything"}
        self.assertEqual(names, {"ras-anything"})

    def _assert_uninstalled(self, home: Path) -> None:
        shim = home / ".local" / "bin" / "ras-anything"
        self.assertFalse(shim.exists(), f"shim still present: {shim}")
        self.assertFalse((home / ".local" / "share" / "ras-anything" / "venv").exists())
        self.assertFalse((home / ".claude" / "skills" / "ras-anything" / "SKILL.md").exists())
        self.assertFalse((home / ".codex" / "skills" / "ras-anything" / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
