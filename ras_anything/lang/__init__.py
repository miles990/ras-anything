"""Language adapters and which one a command belongs to.

To add a language: write lang/<name>.py with a LangAdapter subclass and lang/<name>_agent/ that follows
docs/ARCHITECTURE.md; discover() finds it, nothing here lists it (CONTRIBUTING.md has the checklist). A language can
also live in a package of its own: it registers its adapter class under the entry-point group
contracts.LANGUAGE_ENTRY_POINT_GROUP and ras-anything finds it without any change here (docs/adr/0002).
RAS_ANYTHING_LANGUAGES_OFF=java,go switches languages off by name (docs/adr/0003).
"""

from __future__ import annotations

import sys
from importlib import metadata

from ..contracts import LANGUAGE_ENTRY_POINT_GROUP
from ..paths import UserError
import importlib
import inspect
import pkgutil

from .base import LangAdapter, program_stem

def discover() -> list[LangAdapter]:
    """The built-in adapters: every concrete LangAdapter subclass defined in a module of this package (docs/adr/0003).
    Adding a language adds files (lang/<name>.py, lang/<name>_agent/); nothing here lists them. Order: `priority`,
    then name; a command goes to the first adapter whose detect() says yes."""
    found: dict[str, LangAdapter] = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_") or info.name == "base" or info.ispkg:
            continue
        mod = importlib.import_module("%s.%s" % (__name__, info.name))
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if cls.__module__ == mod.__name__ and issubclass(cls, LangAdapter) and not inspect.isabstract(cls) and cls.name:
                found.setdefault(cls.name, cls())
    return sorted(found.values(), key=lambda a: (a.priority, a.name))


BUILTIN: list[LangAdapter] = discover()


def plugins(group: str = LANGUAGE_ENTRY_POINT_GROUP) -> list[LangAdapter]:
    """Adapters other installed packages register (entry points). A broken plugin is reported and skipped,
    never fatal; a plugin can't replace a built-in language (same name: the built-in wins)."""
    taken = {a.name for a in BUILTIN}
    found: list[LangAdapter] = []
    try:
        eps = metadata.entry_points(group=group)
    except Exception:  # pragma: no cover - broken metadata of some other distribution
        return found
    for ep in sorted(eps, key=lambda e: e.name):
        try:
            cls = ep.load()
            adapter = cls() if isinstance(cls, type) else cls
            if not isinstance(adapter, LangAdapter):
                raise TypeError("%s is not a LangAdapter" % ep.value)
        except Exception as e:
            print("ras-anything: skipped language plugin %r: %s" % (ep.name, e), file=sys.stderr)
            continue
        if adapter.name in taken:
            print("ras-anything: skipped language plugin %r: %r is built in" % (ep.name, adapter.name), file=sys.stderr)
            continue
        taken.add(adapter.name)
        found.append(adapter)
    return found


def switched_off(env: str | None = None) -> set[str]:
    """Language names turned off with RAS_ANYTHING_LANGUAGES_OFF (comma-separated)."""
    import os

    raw = os.environ.get("RAS_ANYTHING_LANGUAGES_OFF", "") if env is None else env
    return {n.strip() for n in raw.split(",") if n.strip()}


def enabled(adapters: list[LangAdapter], off: set[str]) -> list[LangAdapter]:
    return [a for a in adapters if a.name not in off]


ALL_ADAPTERS: list[LangAdapter] = BUILTIN + plugins()
ADAPTERS: list[LangAdapter] = enabled(ALL_ADAPTERS, switched_off())

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
    if not ADAPTERS:
        raise UserError("every language is switched off", "RAS_ANYTHING_LANGUAGES_OFF=%s" % ",".join(sorted(switched_off())),
                        "unset RAS_ANYTHING_LANGUAGES_OFF, or leave the app's language out of it")
    raise UserError("don't know how to watch %r" % (cmd[0] if cmd else ""), supported + (("\n  " + hint) if hint else ""),
                    "scan -- " + ADAPTERS[0].example)


def by_name(name: str | None) -> LangAdapter:
    """The adapter that made a scan or a recording (files from before v0.2 don't say: Python)."""
    for a in ADAPTERS:
        if a.name == (name or "python"):
            return a
    if name in switched_off():
        raise UserError("this project was scanned as %r, which is switched off" % name,
                        "RAS_ANYTHING_LANGUAGES_OFF=%s" % ",".join(sorted(switched_off())), "unset RAS_ANYTHING_LANGUAGES_OFF")
    raise UserError("this project was scanned as %r, which this version can't handle" % name,
                    "supported: " + ", ".join(a.name for a in ADAPTERS),
                    "scan -- " + (ADAPTERS[0].example if ADAPTERS else "python app.py"))
