"""Runs the user's command with the in-process agent attached and collects what it saw."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .paths import INJECT_DIR, PKG_DIR


class RunResult:
    def __init__(self, code, stdout, stderr, wall, data, timed_out=False):
        self.code, self.stdout, self.stderr, self.wall, self.data = code, stdout, stderr, wall, data
        self.timed_out = timed_out

    @property
    def hang(self) -> bool:
        u = (self.data or {}).get("uncaught") or {}
        return self.timed_out or bool(u.get("hang"))


def project_root_for(cmd: list[str], cwd: Path) -> Path:
    """The folder whose functions count as "yours": the script's folder for `python app.py`,
    else the working directory."""
    for i, a in enumerate(cmd[1:], 1):
        if a == "-m":
            return cwd
        if a.endswith(".py") and not a.startswith("-"):
            return (cwd / a).resolve().parent
    return cwd


def run(cmd: list[str], cfg: dict, cwd: Path, timeout: float = 300.0, env_extra: dict | None = None,
        root: Path | None = None) -> RunResult:
    root = root or project_root_for(cmd, cwd)
    fd, out_path = tempfile.mkstemp(prefix="ras-", suffix=".json")
    os.close(fd)
    cfg = dict(cfg, out=out_path, root=str(root), exclude=[str(PKG_DIR)])
    env = dict(os.environ)
    env.pop("RAS_ANYTHING_CFG", None)
    env.update(env_extra or {})
    pp = [str(INJECT_DIR), str(PKG_DIR.parent)]
    if env.get("PYTHONPATH"):
        pp.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pp)
    env["RAS_ANYTHING_CFG"] = json.dumps(cfg)
    env.setdefault("PYTHONHASHSEED", "0")
    t0 = time.perf_counter()
    timed_out = False
    try:
        p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout)
        code, so, se = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        code, so, se = -9, _txt(e.stdout), _txt(e.stderr)
    except FileNotFoundError as e:
        os.unlink(out_path)
        from .paths import UserError

        raise UserError("can't run %s: %s" % (shlex.join(cmd), e.strerror),
                        "the command must start your app (e.g. python app.py)", "doctor")
    wall = time.perf_counter() - t0
    try:
        data = json.loads(Path(out_path).read_text() or "null")
    except Exception:
        data = None
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    return RunResult(code, so, se, wall, data, timed_out)


def _txt(b) -> str:
    if b is None:
        return ""
    return b.decode(errors="replace") if isinstance(b, bytes) else b


def python_cmd(cmd: list[str]) -> list[str]:
    """`python` may not be on PATH (python3-only systems): use this interpreter then."""
    if cmd and cmd[0] == "python":
        from shutil import which

        if which("python") is None:
            return [sys.executable] + cmd[1:]
    return cmd
