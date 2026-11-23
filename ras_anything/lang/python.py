"""Python: everything ras-anything knows about the language, its launchers and its source code.

The half that runs inside the app's process is agents/python/ (loaded by sitecustomize via PYTHONPATH).
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import textwrap
from functools import lru_cache
from pathlib import Path

from .. import signals
from ..paths import PKG_DIR, UserError, load_local, resolve_in_project, safe_name
from . import python_static as static
from .base import LangAdapter, program_stem

AGENT_DIR = PKG_DIR / "agents" / "python"
INJECT_DIR = AGENT_DIR / "_inject"

# Tools that run a command inside a project's own environment: `uv run app.py`, `poetry run python app.py`.
ENV_RUNNERS = ("uv", "poetry", "pipenv", "pdm", "hatch", "rye")


def is_python(program: str) -> bool:
    return re.fullmatch(r"pyw?|(python|pypy)w?[0-9]*(\.[0-9]+)*t?", program_stem(program)) is not None


@lru_cache(maxsize=32)
def _env_python(runner: str, sub: str, cwd: str) -> str:
    shown = "%s %s" % (runner, sub)
    try:
        p = subprocess.run([runner, sub, "python", "-c", "import sys; print(sys.executable)"], cwd=cwd,
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UserError("can't run %s: %s" % (shown, e), "ras-anything asks it which Python your project uses", "doctor")
    lines = p.stdout.strip().splitlines()
    if p.returncode or not lines:
        raise UserError("%s python failed (exit %d)" % (shown, p.returncode), p.stderr.strip()[-300:], "doctor")
    return lines[-1].strip()


class PythonAdapter(LangAdapter):
    name = "python"
    label = "Python"
    launchers = "python, python3, pypy, py, or uv / poetry / pipenv / pdm / hatch / rye run"
    example = "python app.py"
    boundaries = "urlopen, requests, subprocess.run"
    fence = "python"
    hardened_name = "hardened.py"
    private_env = ("PYTHONPATH", "PYTHONHASHSEED")
    bug_exceptions = frozenset({
        "TypeError", "KeyError", "ValueError", "AttributeError", "IndexError", "NameError", "ZeroDivisionError",
        "AssertionError", "UnboundLocalError", "SyntaxError", "ImportError"})

    # ---------------------------------------------------------------- commands

    def detect(self, cmd):
        return is_python(cmd[0]) or (program_stem(cmd[0]) in ENV_RUNNERS and cmd[1:2] == ["run"])

    def resolve(self, cmd, cwd):
        if program_stem(cmd[0]) in ENV_RUNNERS:
            return self._unwrap(cmd, cwd)
        if cmd[0] == "python" and shutil.which("python") is None:  # python3-only systems
            return [sys.executable] + cmd[1:]
        return list(cmd)

    def _unwrap(self, cmd, cwd):
        shown = " ".join(cmd[:2])
        rest = cmd[2:]
        if rest and is_python(rest[0]):
            rest = rest[1:]
        if not rest:
            raise UserError("name the script after %s" % shown, "ras-anything needs to know which app to run",
                            "scan -- %s app.py" % shown)
        if rest[0] != "-m" and not rest[0].endswith(".py"):
            raise UserError("can't tell which Python code %s starts" % " ".join(cmd[:3]),
                            "run it as  %s python <script>.py  or  %s python -m <module>" % (shown, shown),
                            "scan -- %s python app.py" % shown)
        return [_env_python(cmd[0], cmd[1], str(cwd))] + rest

    def project_root(self, cmd, cwd):
        """The script's folder for `python app.py`, else the working directory."""
        for a in cmd[1:]:
            if a == "-m":
                return cwd
            if a.endswith(".py") and not a.startswith("-"):
                return (cwd / a).resolve().parent
        return cwd

    def attach(self, cmd, env):
        env = dict(env)
        env["PYTHONPATH"] = os.pathsep.join([str(INJECT_DIR), str(PKG_DIR.parent)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        env.setdefault("PYTHONHASHSEED", "0")
        return cmd, env

    def interpreter(self, project, meta):
        """The app's Python: the one that ran it here, else the command's own, else this one."""
        exe = load_local(project).get("executable")
        if exe and os.path.exists(exe):
            return exe
        cmd = meta.get("cmd") or []
        try:
            prog = self.resolve(cmd, project)[0] if cmd else ""
        except UserError:
            prog = ""
        if prog and is_python(prog):
            found = str(project / prog) if "/" in prog else shutil.which(prog)
            if found and os.path.exists(found):
                return found
        return sys.executable

    # ---------------------------------------------------------------- source code

    def describe(self, fn):
        fi = static.load(fn["file"])
        info = fi.functions.get(fn["line"]) if fi else None
        if info is None:
            return None
        a = fi.analyze(info)
        in_main = fn["module"] == "__main__"
        mod = Path(fn["file"]).stem if fn["module"] in ("__main__", "") else fn["module"]
        return {"id": "%s.%s" % (mod, info["qualname"]), "module": mod if in_main else fn["module"],
                "qualname": info["qualname"], "name": fn["name"], "kind": info["kind"], "async": info["async"],
                "line": info["line"], "in_main": in_main, "signals": a["signals"],
                "static_risk": signals.risk(a["signals"]), "refused": self._refused(info, fi, in_main)}

    def _refused(self, info, fi, in_main):
        if info["kind"] == "nested":
            return "nested function (top-level functions only)"
        if info["kind"] in ("method", "classmethod"):
            return "%s of a class (top-level functions and @staticmethod only)" % info["kind"]
        if info["async"]:
            return "async function (not supported for Python yet)"
        if in_main and not fi.has_main_guard:
            return "in the entry script with no `if __name__ == \"__main__\"` guard"
        return None

    def hardenability(self, d):
        if d.get("refused"):
            return d["refused"]
        if d.get("gen"):
            return "generator (not supported yet)"
        if d.get("arg_ok") is False:
            return "arguments can't be stored as JSON, so calls can't be recorded"
        if d.get("ret_ok") is False:
            return "return value can't be stored as JSON, so results can't be compared"
        if not d["boundary"]:
            return "makes no dependency calls"
        return None

    def source(self, fn, project):
        fi = static.load(str(resolve_in_project(fn["file"], project)))
        info = fi.functions[fn["first_line"]]
        node = info["node"]
        lines = fi.src.splitlines()
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        src = "\n".join(lines[first - 1: node.end_lineno])
        src = textwrap.dedent(src)
        if info["kind"] == "staticmethod":
            src = "\n".join(ln for ln in src.splitlines() if ln.strip() != "@staticmethod")
        return src

    def stub(self, fn, project):
        """The module's imports, the module-level names the function uses,
        a fallback declaration, and the original function."""
        fi = static.load(str(resolve_in_project(fn["file"], project)))
        node = fi.functions[fn["first_line"]]["node"]
        imports = [ast.get_source_segment(fi.src, n) for n in fi.tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        imports = [i for i in imports if i and "__future__" not in i]
        defined = set()
        for n in fi.tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(n.name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name):
                            defined.add(x.id)
        used = {x.id for x in ast.walk(node) if isinstance(x, ast.Name)}
        from_mod = sorted((used & defined) - {fn["name"]})
        mod = fn["module"]
        lines = [
            '"""Hardened %s, written for ras-anything (see TASK.md). Swapped in by `ras-anything enable`."""' % fn["id"],
            "",
            "from __future__ import annotations",
            "",
            *imports,
            "",
            "from ras_anything.runtime import RasError, CircuitBreaker, call_with_timeout, is_transient, log_event, retry",
        ]
        if from_mod:
            lines.append("from %s import %s  # module-level names the original uses" % (mod, ", ".join(from_mod)))
        lines += [
            "",
            "# What this function returns when it degrades on purpose (dependency down, budget spent).",
            "# A value, or a callable taking the same arguments (lambda *a, **k: None declares None).",
            "# RAS_FALLBACK = None means no fallback: raise a RasError instead.",
            "RAS_FALLBACK = None",
            "",
            "",
            self.source(fn, project),
            "",
        ]
        return "\n".join(lines)

    def callers(self, fn, root, limit=4):
        out = []
        pat = re.compile(r"\b%s\s*\(" % re.escape(fn["name"]))
        for path in sorted(root.rglob("*.py")):
            if ".ras" in path.parts or "_ras_hardened" in path.parts or "site-packages" in str(path):
                continue
            try:
                lines = path.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for i, ln in enumerate(lines):
                if pat.search(ln) and not ln.lstrip().startswith("def "):
                    lo, hi = max(0, i - 2), min(len(lines), i + 5)
                    out.append("# %s:%d\n%s" % (path.relative_to(root), i + 1, "\n".join(lines[lo:hi])))
                    if len(out) >= limit:
                        return out
        return out

    def task_texts(self, fn):
        return {
            "kind": "Python function",
            "timeouts": "Every boundary call needs a timeout (`urlopen(..., timeout=t)`,\n   `subprocess.run(..., timeout=t)`, `requests.get(..., timeout=t)`)",
            "transient": "EAGAIN, a truncated/undecodable body from a 2xx, a subprocess exit 75",
            "raise": '`raise RasError("...", cause=e, context={...}) from e`',
            "declare": "Declare it at module level as `RAS_FALLBACK` (a value, or a callable taking the same arguments;\n"
                       "   to declare `None` as the fallback use `RAS_FALLBACK = lambda *args, **kwargs: None`).",
            "swallow": "`except Exception: return None`",
            "errors": "Log each give-up with `log_event(\"...\", **context)` (JSON line on stderr,\n"
                      "   with a trace id) and chain causes (`raise ... from e`).",
            "equal": "`==`",
            "helpers": """## Helpers (`from ras_anything.runtime import ...`, standard library only)

- `retry(fn, *, attempts=3, base=0.1, cap=2.0, budget_s=None, retry_on=is_transient)`: calls `fn()`;
  full-jitter backoff, honours Retry-After, stops before crossing `budget_s`, re-raises the last
  error unchanged. `retry_on` may be a predicate or a tuple of exception types.
- `is_transient(exc)`: the transient rule from point 3 (not truncated bodies: add `ValueError` /
  `json.JSONDecodeError` yourself when a 2xx body fails to parse).
- `call_with_timeout(fn, timeout_s, *args)`: for code that has no timeout parameter.
- `CircuitBreaker(fail_threshold, cooldown_s)`: `.call(fn, *args)`; raises `CircuitOpenError` while open.
- `RasError(message, *, cause=None, context=None)`: structured error with a trace id.
- `log_event(event, **fields)`: one JSON line on stderr.
""",
        }

    # ---------------------------------------------------------------- record / replay

    def record_target(self, fn, project):
        return {"file": str(resolve_in_project(fn["file"], project)), "line": fn["first_line"], "name": fn["name"]}

    def record_meta(self, fn, data, project):
        return {"sys_path": _portable_sys_path(data.get("sys_path") or [], project),
                "type_modules": data.get("type_modules") or []}

    def refuse_hardened(self, path):
        names = static.forbidden_imports(path)
        if not names:
            return None
        return ("%s imports %s. Hardened code may use ras_anything.runtime only: the rest of ras-anything is the harness "
                "that judges it." % (self.hardened_name, ", ".join(names)))

    def worker_cmd(self, exe, job):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(PKG_DIR.parent)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])))
        env.pop("RAS_ANYTHING_CFG", None)
        return [exe, "-m", "ras_anything.agents.python.worker", str(job)], env

    # ---------------------------------------------------------------- enable

    def markers(self, fid):
        return "# >>> ras-anything:%s >>>" % fid, "# <<< ras-anything:%s <<<" % fid

    def block(self, fid, meta, copy_rel, vres):
        start, end = self.markers(fid)
        qual = meta["qualname"]
        if "." in qual:
            cls, name = qual.split(".", 1)
            bind = "    %s.%s = staticmethod(_ras_fn)" % (cls, name)
        else:
            name = qual
            bind = "    %s = _ras_fn" % name
        return _BLOCK.format(start=start, end=end, fid=fid, date=vres.get("verified_at", "?")[:10], total=vres["happy"]["total"],
                             before=100 * vres["original"]["availability"], after=100 * vres["hardened"]["availability"],
                             copy=copy_rel, mod=safe_name(fid), name=name, bind=bind)

    def insertion(self, path, text, block, eol):
        fi = static.FileInfo(str(path))
        if fi.has_main_guard:  # must bind before the entry point runs
            lines = text.splitlines(keepends=True)
            return sum(len(ln) for ln in lines[: fi.main_guard_line - 1]), block + eol + eol
        return len(text), (eol if text.endswith(eol) else eol + eol) + eol + block

    def copy_name(self, fid):
        return safe_name(fid) + ".py"

    def runtime_file(self):
        return PKG_DIR / "runtime.py", "_runtime.py"

    # ---------------------------------------------------------------- people

    def doctor(self):
        out = []
        v = sys.version_info
        out.append((v >= (3, 10), "python %d.%d.%d" % v[:3], "" if v >= (3, 10) else "ras-anything needs Python 3.10+"))
        try:
            import requests

            out.append((True, "requests", "%s (intercepted too)" % requests.__version__))
        except ImportError:
            out.append((None, "requests", "not installed (optional: urllib and subprocess are covered)"))
        return out


def _portable_sys_path(entries: list[str], project: Path) -> list[str]:
    """Import folders the app added, relative to the project. The interpreter's own and
    ras-anything's are left out: they are there anyway when verify runs."""
    skip = [Path(p).resolve() for p in (sys.prefix, sys.base_prefix, "/usr/lib", "/usr/local/lib", PKG_DIR.parent)]
    proj = project.resolve()
    out = []
    for e in entries:
        p = Path(e or ".")
        p = (proj / p).resolve()
        inside = p == proj or p.is_relative_to(proj)
        if not p.is_dir() or (not inside and any(p == s or p.is_relative_to(s) for s in skip)) or "site-packages" in p.parts:
            continue
        rel = Path(os.path.relpath(p, proj)).as_posix()
        if rel not in out:
            out.append(rel)
    return out


_BLOCK = '''{start}
# Hardened by ras-anything; verified {date} on {total} recorded calls
# (availability under faults {before:.1f}% -> {after:.1f}%).
# Undo: `ras-anything disable {fid}`. RAS_ANYTHING_DISABLE=1 runs the original.
def _ras_anything_load():
    import importlib.util, os, sys
    if os.environ.get("RAS_ANYTHING_DISABLE") == "1":
        return None
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            import ras_anything.runtime  # noqa: F401
        except ImportError:  # not installed here: use the copy enable put next to the hardened code
            import types
            rt = importlib.util.spec_from_file_location("ras_anything.runtime", os.path.join(here, "_ras_hardened", "_runtime.py"))
            sys.modules.setdefault("ras_anything", types.ModuleType("ras_anything"))
            sys.modules["ras_anything.runtime"] = m = importlib.util.module_from_spec(rt)
            rt.loader.exec_module(m)
        path = os.path.join(here, {copy!r})
        spec = importlib.util.spec_from_file_location("_ras_hardened.{mod}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, {name!r})
    except Exception as e:  # keep the app running on the original, loudly
        sys.stderr.write("ras-anything: hardened {fid} not loaded, running the original: %r\\n" % (e,))
        return None


_ras_fn = _ras_anything_load()
if _ras_fn is not None:
{bind}
del _ras_fn, _ras_anything_load
{end}
'''
