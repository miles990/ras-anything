"""harden: record real calls of one function (with their dependency I/O) and set up the agent's task."""

from __future__ import annotations

import ast
import os
import pickle
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from . import faults, static, ui
from .paths import UserError, find_project, harden_dir, load_json, ras_dir, save_json
from .runner import python_cmd, run

DEFAULT_BUDGET_S = 5.0


def load_scan(project: Path) -> dict:
    scan = load_json(project / ".ras" / "scan.json")
    if scan is None:
        raise UserError("no scan yet in %s" % project, "harden needs to know which function to record",
                        "scan -- python app.py")
    return scan


def pick(scan: dict, fid: str | None) -> dict:
    fns = scan["functions"]
    if fid is None:
        top = next((f for f in fns if f["hardenable"]), None)
        if top is None:
            raise UserError("scan found no function to harden", "nothing in your project both calls a dependency "
                            "and can be swapped", "scan -- python app.py")
        return top
    for f in fns:
        if f["id"] == fid:
            if not f["hardenable"]:
                raise UserError("%s can't be hardened yet: %s" % (fid, f["verdict"]), "", "scan -- " + scan.get("display_cmd", ""))
            return f
    ids = ", ".join(f["id"] for f in fns if f["hardenable"]) or "none"
    raise UserError("no function %r in the last scan" % fid, "hardenable: " + ids, "status")


def function_source(fn: dict) -> str:
    fi = static.load(fn["file"])
    info = fi.functions[fn["first_line"]]
    node = info["node"]
    lines = fi.src.splitlines()
    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
    src = "\n".join(lines[first - 1: node.end_lineno])
    src = textwrap.dedent(src)
    if info["kind"] == "staticmethod":
        src = "\n".join(ln for ln in src.splitlines() if ln.strip() != "@staticmethod")
    return src


def stub(fn: dict, project: Path) -> str:
    """hardened.py to start from: the module's imports, the module-level names the function uses,
    a fallback declaration, and the original function."""
    fi = static.load(fn["file"])
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
        function_source(fn),
        "",
    ]
    return "\n".join(lines)


def callers(fn: dict, root: Path, limit: int = 4) -> list[str]:
    """Lines in the project that call the function, with a little context."""
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
                out.append("%s:%d\n%s" % (path.relative_to(root), i + 1, "\n".join(lines[lo:hi])))
                if len(out) >= limit:
                    return out
    return out


def _short(v, n=240) -> str:
    r = repr(v)
    return r if len(r) <= n else r[: n - 1] + "…"


def task_md(fn: dict, meta: dict, calls: list, scan: dict, root: Path) -> str:
    fid = fn["id"]
    budget = meta["budget_s"]
    reqs = {}
    for c in calls:
        for io in c["io"]:
            r = io["req"]
            k = "%s %s" % (r["method"] or r["kind"], re.sub(r"\?.*", "?…", r["url"]) if r["url"] else " ".join(r["argv"] or [])[:80])
            reqs[k] = reqs.get(k, 0) + 1
    samples = []
    for c in calls[:3]:
        args, kwargs = pickle.loads(c["args"])
        res = ("raises %s: %s" % (c["exc"]["type"], c["exc"]["msg"])) if "exc" in c else _short(pickle.loads(c["result"]))
        io = "; ".join("%s %s -> %s" % (x["req"]["method"] or "run", (x["req"]["url"] or " ".join(x["req"]["argv"] or []))[:90],
                                         x["out"].get("status", x["out"].get("returncode", "exception")))
                       for x in c["io"])
        samples.append("- `%s(%s)` → %s\n  dependency I/O: %s" % (
            fn["name"], ", ".join([_short(a, 80) for a in args] + ["%s=%s" % (k, _short(v, 60)) for k, v in kwargs.items()]),
            res, io or "none"))
    sigs = "\n".join("- " + s for s in fn["signals"].values()) or "- (none)"
    dyn = fn.get("chaos")
    dyn_line = ("- under seeded chaos the app failed in %d/%d runs where this function's dependency calls were faulted"
                % (dyn["failed"], dyn["runs"])) if dyn else ""
    call_ctx = "\n\n".join("```python\n# %s\n```" % c for c in callers(fn, root)) or "(no other callers found)"
    matrix = "\n".join("| `%s` | %s |" % (k, faults.DESCRIBE[k]) for k in faults.MATRIX + ("mixed",))
    return f"""# Harden `{fid}`

You are hardening one Python function for **reliability, availability and serviceability**.
Edit **only `hardened.py` in this folder**. Keep the function name `{fn['name']}` and its signature.
When you are done, `ras-anything verify {fid}` must print PASS. Run it yourself (it works from this
folder) and iterate until it passes. Do not enable anything; do not edit the original source,
`calls.pkl` or `meta.json`.

## The function

`{fid}` in `{os.path.relpath(fn['file'], root)}` line {fn['line']}. Recorded **{len(calls)} real calls** from
`{meta['display_cmd']}`. Dependency requests it made:
{chr(10).join("- %d× `%s`" % (n, k) for k, n in reqs.items())}

What the scan found:
{sigs}
{dyn_line}

```python
{function_source(fn)}
```

How callers use the result (a fallback must be something they already handle):

{call_ctx}

Recorded examples:
{chr(10).join(samples)}

## The contract (what `verify` enforces)

1. **Same happy path.** With healthy dependencies, return exactly what the original returned for
   every recorded call (compared with `==`). Make the same requests (same method, URL, body); you
   may add headers (e.g. `Idempotency-Key`) and must pass `timeout=`. Where the original raised,
   raise the same exception type (or a subclass).
2. **Bounded latency: {budget:g} s per call**, everything included: timeouts, backoff sleeps,
   Retry-After waits. Every boundary call needs a timeout (`urlopen(..., timeout=t)`,
   `subprocess.run(..., timeout=t)`, `requests.get(..., timeout=t)`); a call with no timeout that
   hangs is a hard fail. Budget the attempts so worst case stays under {budget:g} s.
3. **Retry only transient failures**: timeouts, connection errors, HTTP 408/425/429/500/502/503/504
   (honour `Retry-After`), EAGAIN, a truncated/undecodable body from a 2xx, a subprocess exit 75. Exponential
   backoff with full jitter. **Never retry other 4xx** or programming errors.
4. **Idempotency.** Never send a POST/PUT/PATCH/DELETE twice unless every attempt carries the same
   `Idempotency-Key` header, derived deterministically from the logical operation (e.g. the invoice
   id), not random per attempt. A timed-out POST may already have taken effect.
5. **Degrade explicitly, never silently.** When the dependency stays down, either raise a structured
   error (`raise RasError("...", cause=e, context={{...}}) from e`) or return the declared fallback.
   Declare it at module level as `RAS_FALLBACK` (a value, or a callable taking the same arguments;
   to declare `None` as the fallback use `RAS_FALLBACK = lambda *args, **kwargs: None`).
   Any other value that differs from the happy result counts as **silent wrong data** and fails.
   Swallowing errors (`except Exception: return None`) is the defect you are fixing.
6. **Serviceable errors.** Log each give-up with `log_event("...", **context)` (JSON line on stderr,
   with a trace id) and chain causes (`raise ... from e`).

`verify` replays every recorded call against recorded dependency answers (no network), on a
**virtual clock** (sleeps and timeouts cost no real time and are measured exactly), then replays them
again under this fault matrix, for the original and for your version, with the same seeds:

| fault | what happens at the boundary |
|---|---|
{matrix}

Gates: happy replay 100% equal · no `wrong` results · no hangs · p99 latency ≤ {budget:g} s ·
duplicate non-idempotent side effects ≤ original · availability (ok + degraded) strictly higher
than the original.

## Helpers (`from ras_anything.runtime import ...`, standard library only)

- `retry(fn, *, attempts=3, base=0.1, cap=2.0, budget_s=None, retry_on=is_transient)`: calls `fn()`;
  full-jitter backoff, honours Retry-After, stops before crossing `budget_s`, re-raises the last
  error unchanged. `retry_on` may be a predicate or a tuple of exception types.
- `is_transient(exc)`: the transient rule from point 3 (not truncated bodies: add `ValueError` /
  `json.JSONDecodeError` yourself when a 2xx body fails to parse).
- `call_with_timeout(fn, timeout_s, *args)`: for code that has no timeout parameter.
- `CircuitBreaker(fail_threshold, cooldown_s)`: `.call(fn, *args)`; raises `CircuitOpenError` while open.
- `RasError(message, *, cause=None, context=None)`: structured error with a trace id.
- `log_event(event, **fields)`: one JSON line on stderr.

Write it like production code: small, readable, commented where it matters. Then run
`ras-anything verify {fid}` and fix until it passes.
"""


def env_diff(child_env: dict) -> dict:
    skip = ("RAS_ANYTHING", "PYTHONPATH", "PYTHONHASHSEED")
    return {k: v for k, v in (child_env or {}).items()
            if not k.startswith(skip) and os.environ.get(k) != v}


def harden(fid: str | None = None, agent: str | None = None, model: str | None = None, budget: float | None = None,
           max_calls: int = 500, force: bool = False, extra_prompt: str = "", quiet: bool = False) -> dict:
    project = find_project()
    scan = load_scan(project)
    fn = pick(scan, fid)
    fid = fn["id"]
    d = harden_dir(project, fid)
    d.mkdir(parents=True, exist_ok=True)
    if not quiet:
        ui.headline("harden", "%s %s recording up to %d calls" % (fid, ui.DOT, max_calls))
    calls_path = d / "calls.pkl"
    r = run(python_cmd(scan["cmd"]), {"mode": "record", "max_calls": max_calls, "calls_path": str(calls_path),
                          "target": {"file": fn["file"], "line": fn["first_line"], "name": fn["name"]}},
            Path(scan["cwd"]), root=Path(scan["root"]))
    if r.data is None or not calls_path.exists():
        raise UserError("recording failed (exit %s)" % r.code, (r.stderr or "").strip()[-300:], "doctor")
    calls = pickle.loads(calls_path.read_bytes())
    if not calls:
        raise UserError("%s wasn't called while the workload ran" % fid, "is the command the same as for scan?",
                        "scan -- " + scan.get("display_cmd", ""))
    snap = r.data.get("snapshot") or {}
    old = load_json(d / "meta.json", {}) or {}
    meta = {
        "id": fid, "module": fn["module"], "qualname": fn["qualname"], "name": fn["name"], "kind": fn["kind"],
        "file": fn["file"], "first_line": fn["first_line"], "in_main": fn["in_main"],
        "cmd": scan["cmd"], "display_cmd": scan.get("display_cmd", shlex.join(scan["cmd"])), "cwd": scan["cwd"],
        "sys_path": r.data.get("sys_path", []),
        "executable": r.data.get("executable"),
        "env": env_diff(snap.get("env")),
        "globals": {k: v for k, v in (snap.get("globals") or {}).items() if not isinstance(v, bytes)},
        "budget_s": budget or old.get("budget_s") or DEFAULT_BUDGET_S,
        "recorded": len(calls), "seen": r.data.get("seen"), "skipped": r.data.get("skipped"),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": sys.version.split()[0],
    }
    save_json(d / "meta.json", meta)
    hp = d / "hardened.py"
    kept = hp.exists() and not force
    if not kept:
        hp.write_text(stub(fn, project))
    (d / "TASK.md").write_text(task_md(fn, meta, calls, scan, Path(scan["root"])))
    nio = sum(len(c["io"]) for c in calls)
    if not quiet:
        print("  %s recorded %d calls (of %d seen) with %d dependency exchanges %s %s" % (
            ui.good(ui.OK), len(calls), meta["seen"] or len(calls), nio, ui.DOT,
            ui.dim("calls.pkl %.0f KB" % (calls_path.stat().st_size / 1024))))
        print("  %s wrote %s" % (ui.good(ui.OK), os.path.relpath(d / "TASK.md")))
        print("  %s %s %s" % (ui.good(ui.OK), "kept your" if kept else "wrote stub", os.path.relpath(hp)))
    result = {"id": fid, "dir": str(d), "recorded": len(calls), "agent": None}
    if agent:
        code = run_agent(agent, d, project, model, extra_prompt, quiet)
        result["agent"] = {"name": agent, "exit": code}
        if not quiet:
            if code == 0:
                print("  %s agent finished" % ui.good(ui.OK))
            else:
                print("  %s agent exited with %d (log: %s)" % (ui.bad(ui.FAIL), code, os.path.relpath(d / "agent.log")))
    if not quiet:
        if agent:
            ui.next_hint("verify %s" % fid)
        else:
            print()
            print("  hand %s to your coding agent, or rerun with --agent claude|codex|<cmd>" % os.path.relpath(d / "TASK.md"))
            ui.next_hint("verify %s" % fid)
    return result


def agent_command(agent: str, project: Path, model: str | None):
    if agent == "claude":
        cmd = ["claude", "-p", "--permission-mode", "acceptEdits",
               "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(ras-anything verify:*),Bash(ras-anything status:*)",
               "--add-dir", str(project)]
        if model:
            cmd += ["--model", model]
        return cmd, False
    if agent == "codex":
        cmd = ["codex", "exec", "--full-auto", "--skip-git-repo-check"]
        if model:
            cmd += ["-m", model]
        return cmd + ["-"], False
    return agent, True


def run_agent(agent: str, d: Path, project: Path, model: str | None = None, extra: str = "", quiet: bool = False) -> int:
    cmd, shell = agent_command(agent, project, model)
    exe = cmd.split()[0] if shell else cmd[0]
    if not shell and shutil.which(exe) is None:
        raise UserError("%s is not on PATH" % exe, "install it, or pass any command that reads a prompt on stdin",
                        "doctor")
    prompt = (d / "TASK.md").read_text() + (("\n\n" + extra) if extra else "")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    if not quiet:
        print("  %s running %s in %s %s" % (ui.accent(ui.ARROW), agent if not shell else shlex.quote(agent),
                                           os.path.relpath(d), ui.dim("(log: agent.log)")), flush=True)
    t0 = time.perf_counter()
    with open(d / "agent.log", "a") as log:
        log.write("\n===== %s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), cmd if shell else shlex.join(cmd)))
        log.flush()
        p = subprocess.run(cmd, shell=shell, cwd=str(d), input=prompt, text=True, stdout=log, stderr=subprocess.STDOUT,
                           env=env, timeout=3600)
        log.write("\n===== exit %d after %.0fs\n" % (p.returncode, time.perf_counter() - t0))
    return p.returncode
