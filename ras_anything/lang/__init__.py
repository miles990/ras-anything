"""Language adapters and which one a command belongs to.

To add a language: write lang/<name>.py with a LangAdapter subclass and an agent that follows
docs/ARCHITECTURE.md, and list it in ADAPTERS.
"""

from __future__ import annotations

from ..paths import UserError
from .base import LangAdapter, program_stem
from .golang import GoAdapter
from .java import JavaAdapter
from .node import NodeAdapter
from .python import PythonAdapter

ADAPTERS: list[LangAdapter] = [PythonAdapter(), NodeAdapter(), GoAdapter(), JavaAdapter()]

# Recognized, but no adapter here yet.
PLANNED = {"bun": "Bun", "deno": "Deno"}

_TS = "Node 22.6+ runs TypeScript itself: ras-anything scan -- node app.ts"
_HINTS = {
    "npx": _TS, "tsx": _TS, "ts-node": _TS,
    "npm": "start the script's own command instead of npm: ras-anything scan -- node app.js",
    "pnpm": "start the script's own command instead of pnpm: ras-anything scan -- node app.js",
    "yarn": "start the script's own command instead of yarn: ras-anything scan -- node app.js",
    "pytest": "run it as a module: ras-anything scan -- python -m pytest",
    "flask": "run it as a module: ras-anything scan -- python -m flask run",
    "gunicorn": "run a script that drives your app instead: ras-anything scan -- python workload.py",
    "uvicorn": "run a script that drives your app instead: ras-anything scan -- python workload.py",
    "bash": "start the app itself instead of a shell: ras-anything scan -- python app.py",
    "sh": "start the app itself instead of a shell: ras-anything scan -- python app.py",
    "make": "start the app itself instead of make: ras-anything scan -- python app.py",
    "env": "set variables in your shell, then start the app itself: ras-anything scan -- python app.py",
    "go": "start your Go app with go run: ras-anything scan -- go run .",
    "java": "name what it runs: ras-anything scan -- java App.java (or java -cp DIR Main, java -jar app.jar)",
    **{w: "build it, then start the main class itself: ras-anything scan -- java -cp %s Main" % d
       for w, d in (("mvn", "target/classes"), ("mvnw", "target/classes"), ("gradle", "build/classes/java/main"), ("gradlew", "build/classes/java/main"))},
}


def launchers() -> str:
    have = ["%s: %s" % (a.label, a.launchers) for a in ADAPTERS]
    return " · ".join(have)


def detect(cmd: list[str]) -> LangAdapter:
    """The adapter for a run command, from its program (like a shell would read it)."""
    for a in ADAPTERS:
        if cmd and a.detect(cmd):
            return a
    stem = program_stem(cmd[0]) if cmd else ""
    supported = "supported launchers: " + launchers()
    if stem in PLANNED:
        script = next((a for a in cmd[1:] if a.endswith((".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"))), "app.ts")
        raise UserError("%s isn't supported yet" % PLANNED[stem], supported + "\n  if the app also runs on Node.js: "
                        "ras-anything scan -- node %s" % script, "scan -- node " + script)
    hint = _HINTS.get(stem)
    if hint is None and stem.endswith((".py", ".pyw")):
        hint = "start it with its interpreter: ras-anything scan -- python %s" % cmd[0]
    if hint is None and stem.endswith((".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")):
        hint = "start it with its interpreter: ras-anything scan -- node %s" % cmd[0]
    raise UserError("don't know how to watch %r" % (cmd[0] if cmd else ""), supported + (("\n  " + hint) if hint else ""),
                    "scan -- " + ADAPTERS[0].example)


def by_name(name: str | None) -> LangAdapter:
    """The adapter that made a scan or a recording (files from before v0.2 don't say: Python)."""
    for a in ADAPTERS:
        if a.name == (name or "python"):
            return a
    raise UserError("this project was scanned as %r, which this version can't handle" % name,
                    "supported: " + ", ".join(a.name for a in ADAPTERS), "scan -- " + ADAPTERS[0].example)
