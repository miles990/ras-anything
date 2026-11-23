"""Runs the user's command with the language's in-process agent attached and collects what it saw."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from .paths import PKG_DIR, UserError


class RunResult:
    def __init__(self, code, stdout, stderr, wall, data, timed_out=False):
        self.code, self.stdout, self.stderr, self.wall, self.data = code, stdout, stderr, wall, data
        self.timed_out = timed_out

    @property
    def hang(self) -> bool:
        u = (self.data or {}).get("uncaught") or {}
        return self.timed_out or bool(u.get("hang"))


def run(adapter, cmd: list[str], cfg: dict, cwd: Path, timeout: float = 300.0, env_extra: dict | None = None,
        root: Path | None = None) -> RunResult:
    """One run of `cmd` (already resolved by the adapter) in one agent mode. The agent reads cfg from
    RAS_ANYTHING_CFG and leaves its findings in cfg["out"]: see docs/ARCHITECTURE.md."""
    root = root or adapter.project_root(cmd, cwd)
    fd, out_path = tempfile.mkstemp(prefix="ras-", suffix=".json")
    os.close(fd)
    cfg = dict(cfg, protocol=1, out=out_path, root=str(root), cwd=str(cwd), exclude=[str(PKG_DIR)])
    cfg.setdefault("project", str(root))
    env = dict(os.environ)
    env.update(env_extra or {})
    env["RAS_ANYTHING_CFG"] = json.dumps(cfg)
    argv, env = adapter.attach(cmd, env)
    t0 = time.perf_counter()
    timed_out = False
    try:
        p = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout)
        code, so, se = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        code, so, se = -9, _txt(e.stdout), _txt(e.stderr)
    except OSError as e:
        os.unlink(out_path)
        raise UserError("can't run %s: %s" % (shlex.join(cmd), e.strerror),
                        "the command must start your app (e.g. %s)" % adapter.example, "doctor")
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
