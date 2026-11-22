"""Where things live: the project's `.ras/` folder and small JSON helpers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
INJECT_DIR = PKG_DIR / "_inject"


class UserError(Exception):
    """A problem the user can fix; carries the next command to run."""

    def __init__(self, what: str, impact: str = "", nxt: str = ""):
        super().__init__(what)
        self.what, self.impact, self.nxt = what, impact, nxt


def find_project(start: str | os.PathLike | None = None) -> Path:
    """The nearest folder (cwd or a parent) that has a `.ras/`; else cwd."""
    here = Path(start or os.getcwd()).resolve()
    for p in (here, *here.parents):
        if (p / ".ras").is_dir():
            return p
    return here


def ras_dir(project: Path) -> Path:
    d = project / ".ras"
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("# ras-anything scratch (keep the rest: hardened code, verify/bench results)\nruns/\n*.tmp\n")
    return d


def harden_dir(project: Path, fid: str) -> Path:
    return project / ".ras" / "hardens" / fid


def load_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def save_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False, default=str) + "\n")
    os.replace(tmp, path)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe_name(fid: str) -> str:
    return fid.replace(".", "__").replace("/", "_")


def rel_to_project(path: str | os.PathLike, project: Path) -> str:
    """Store path relative to `project` when inside it; otherwise return resolved string."""
    proj = project.resolve()
    p = Path(path)
    if not p.is_absolute():
        p = (proj / p).resolve()
    else:
        p = p.resolve()
    try:
        return p.relative_to(proj).as_posix() or "."
    except ValueError:
        return str(p)


def resolve_in_project(path: str | os.PathLike, project: Path) -> Path:
    """Resolve `path` against `project` and refuse any path (or symlink) escaping `project`."""
    proj = project.resolve()
    raw = Path(path)
    cand = raw if raw.is_absolute() else (proj / raw)
    resolved = cand.resolve()
    if not resolved.is_relative_to(proj):
        raise UserError(
            "path %s escapes project %s" % (path, proj),
            "refusing to read or write outside the project directory",
            "scan",
        )
    return resolved
