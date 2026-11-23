"""Where things live: the project's `.ras/` folder and small JSON helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
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


# --------------------------------------------------------------------------- machine-specific facts
#
# What goes into .ras/*.json is meant to be committed, so it never holds absolute paths. The exact
# command and interpreter of the last scan/record on this machine live in .ras/runs/ (gitignored).


def local_path(project: Path) -> Path:
    return project / ".ras" / "runs" / "launcher.local"


def load_local(project: Path) -> dict:
    try:
        d = load_json(local_path(project), {})
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def save_local(project: Path, **facts) -> None:
    ras_dir(project)
    try:
        save_json(local_path(project), {**load_local(project), **facts})
    except OSError:  # only a convenience: verify falls back to the interpreter on PATH
        pass


def portable_cmd(cmd: list[str], project: Path) -> list[str]:
    """The command without this machine's absolute paths: relative inside the project, else the name."""
    out = []
    for a in cmd:
        if os.path.isabs(a):
            rel = os.path.relpath(a, project)  # not resolved: .venv/bin/python is a symlink out of the project
            a = os.path.basename(a) if rel.startswith("..") else rel
        out.append(a)
    return out


def runnable_cmd(stored: list[str], project: Path) -> list[str]:
    """A stored command as it was typed on this machine, or as stored when it wasn't recorded here."""
    local = load_local(project)
    cmd = local.get("cmd")
    if local.get("portable") == stored and isinstance(cmd, list) and cmd and (
            not os.path.isabs(cmd[0]) or os.path.exists(cmd[0])):
        return cmd
    return stored


def scrub(data, project: Path):
    """`data` with this machine's paths replaced (project -> ".", the tool, Python, home)."""
    subs = []
    for path, name in ((project, "."), (project.resolve(), "."), (PKG_DIR, "ras_anything"), (PKG_DIR.parent, "<ras-anything>"),
                       (sys.prefix, "<python>"), (sys.base_prefix, "<python>"), (Path.home(), "~")):
        if len(str(path)) > 3:
            subs.append((re.compile(re.escape(str(path)) + r"(?![\w.\-])"), name))
    for lib in ("/usr/lib/python3", "/usr/local/lib/python3"):
        subs.append((re.compile(re.escape(lib)), "<python>/lib/python3"))

    def walk(x):
        if isinstance(x, str):
            for rx, name in subs:
                x = rx.sub(lambda m: name, x)
            return x
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [walk(v) for v in x]
        return x

    return walk(data)
