"""harden: record real calls of one function (with their dependency I/O) and set up the agent's task."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import cassette, faults, lang, ui
from .paths import UserError, find_project, harden_dir, load_json, portable_cmd, rel_to_project, resolve_in_project, runnable_cmd, save_json, save_local, scrub
from .runner import run

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


def _argv(req: dict) -> str:
    argv = req.get("argv") or []
    return argv if isinstance(argv, str) else " ".join(a if isinstance(a, str) else cassette.show(a).strip("'") for a in argv)


def task_md(adapter, fn: dict, meta: dict, calls: list, scan: dict, root: Path, project: Path) -> str:
    fid = fn["id"]
    tx, hname = adapter.task_texts(fn), adapter.hardened_file(fn)
    budget = meta["budget_s"]
    reqs = {}
    for c in calls:
        for io in c["io"]:
            r = io["req"]
            k = "%s %s" % (r["method"] or r["kind"], re.sub(r"\?.*", "?…", cassette.text(r["url"], "")) if r["url"] else _argv(r)[:80])
            reqs[k] = reqs.get(k, 0) + 1
    samples = []
    for c in calls[:3]:
        res = ("raises %s: %s" % (cassette.exc_name(c["exc"]), c["exc"]["msg"])) if "exc" in c \
            else cassette.show(c["result"])
        io = "; ".join("%s %s -> %s" % (x["req"]["method"] or "run", (cassette.text(x["req"]["url"], "") or _argv(x["req"]))[:90],
                                         x["out"].get("status", x["out"].get("returncode", "exception")))
                       for x in c["io"])
        samples.append("- `%s(%s)` → %s\n  dependency I/O: %s" % (
            fn["name"], ", ".join([cassette.show(a, 80) for a in c["args"]] +
                                 ["%s=%s" % (k, cassette.show(v, 60)) for k, v in c["kwargs"].items()]),
            res, io or "none"))
    sigs = "\n".join("- " + s for s in fn["signals"].values()) or "- (none)"
    dyn = fn.get("chaos")
    dyn_line = ("- under seeded chaos the app failed in %d/%d runs where this function's dependency calls were faulted"
                % (dyn["failed"], dyn["runs"])) if dyn else ""
    call_ctx = "\n\n".join("```%s\n%s\n```" % (adapter.fence, c) for c in adapter.callers(fn, root)) or "(no other callers found)"
    matrix = "\n".join("| `%s` | %s |" % (k, faults.DESCRIBE[k]) for k in faults.MATRIX + ("mixed",))
    return f"""# Harden `{fid}`

You are hardening one {tx['kind']} for **reliability, availability and serviceability**.
Edit **only `{hname}` in this folder**. Keep the function name `{fn['name']}` and its signature.
When you are done, `ras-anything verify {fid}` must print PASS. Run it yourself (it works from this
folder) and iterate until it passes. Do not enable anything; do not edit the original source,
`calls.json` or `meta.json`.

## The function

`{fid}` in `{os.path.relpath(str(resolve_in_project(fn['file'], project)), root)}` line {fn['line']}. Recorded **{len(calls)} real calls** from
`{meta['display_cmd']}`. Dependency requests it made:
{chr(10).join("- %d× `%s`" % (n, k) for k, n in reqs.items())}

What the scan found:
{sigs}
{dyn_line}

```{adapter.fence}
{adapter.source(fn, project)}
```

How callers use the result (a fallback must be something they already handle):

{call_ctx}

Recorded examples:
{chr(10).join(samples)}

## The contract (what `verify` enforces)

1. **Same happy path.** With healthy dependencies, return exactly what the original returned for
   every recorded call (compared with {tx['equal']}). Make the same requests (same method, URL, body); you
   may add headers (e.g. `Idempotency-Key`) and must pass {tx.get('timeout_arg', '`timeout=`')}. Where the original raised,
   raise the same exception type (or a subclass).
2. **Bounded latency: {budget:g} s per call**, everything included: timeouts, backoff sleeps,
   Retry-After waits. {tx['timeouts']}; a call with no timeout that
   hangs is a hard fail. Budget the attempts so worst case stays under {budget:g} s.
3. **Retry only transient failures**: timeouts, connection errors, HTTP 408/425/429/500/502/503/504
   (honour `Retry-After`), {tx['transient']}. Exponential
   backoff with full jitter. **Never retry other 4xx** or programming errors. Retries must not
   multiply an outage: use a `Policy` (below), whose retry budget and circuit breaker cap them;
   verify reports the attempts per call under faults and with the dependency down.
4. **Idempotency.** Never send a POST/PUT/PATCH/DELETE twice unless every attempt carries the same
   `Idempotency-Key` header, derived deterministically from the logical operation (e.g. the invoice
   id), not random per attempt. A timed-out POST may already have taken effect.
5. **Degrade explicitly, never silently.** When the dependency stays down, either raise a structured
   error ({tx['raise']}) or return the declared fallback.
   {tx['declare']}
   Any other value that differs from the happy result counts as **silent wrong data** and fails.
   Swallowing errors ({tx['swallow']}) is the defect you are fixing.
   A fallback counts only in a call where a dependency request really failed and never got a good
   answer (the recorder watches the boundary; a log line alone proves nothing). The one exception is an open
   circuit: no request is needed if this dependency was seen to fail, and not to answer since, in the
   last 60 s (verify also runs your function through a long outage and recovery, state kept).
6. **Serviceable errors.** {tx['errors']}

`verify` replays every recorded call against recorded dependency answers (no network), on a
**virtual clock** (sleeps and timeouts cost no real time and are measured exactly), then replays them
again under this fault matrix, for the original and for your version, with the same seeds:

| fault | what happens at the boundary |
|---|---|
{matrix}

Gates: happy replay 100% equal · no `wrong` results · no hangs · p99 latency ≤ {budget:g} s ·
duplicate non-idempotent side effects ≤ original · availability (ok + degraded) strictly higher
than the original.

{tx['helpers']}
Write it like production code: small, readable, commented where it matters. Then run
`ras-anything verify {fid}` and fix until it passes.
"""


def env_diff(child_env: dict, project: Path, private: tuple) -> dict:
    """What the app itself put in its environment before the first recorded call (values encoded)."""
    skip = ("RAS_ANYTHING",) + private
    return {k: v for k, v in (child_env or {}).items()
            if not k.startswith(skip) and os.environ.get(k) != cassette.text(v, project)}


def harden(fid: str | None = None, agent: str | None = None, model: str | None = None, budget: float | None = None,
           max_calls: int = 500, force: bool = False, extra_prompt: str = "", quiet: bool = False) -> dict:
    project = find_project()
    scan = load_scan(project)
    fn = pick(scan, fid)
    fid = fn["id"]
    adapter = lang.by_name(scan.get("lang"))
    d = harden_dir(project, fid)
    d.mkdir(parents=True, exist_ok=True)
    if not quiet:
        ui.headline("harden", "%s %s recording up to %d calls" % (fid, ui.DOT, max_calls))
    calls_path = d / "calls.json"
    scan_cwd = resolve_in_project(scan.get("cwd") or ".", project)
    scan_root = resolve_in_project(scan.get("root") or ".", project)
    cmd = adapter.resolve(runnable_cmd(scan["cmd"], project), scan_cwd)
    r = run(adapter, cmd, {"mode": "record", "max_calls": max_calls, "calls_path": str(calls_path),
                           "project": str(project), "target": adapter.record_target(fn, project)},
            scan_cwd, root=scan_root)
    if r.code != 0 or r.data is None or not calls_path.exists():
        raise UserError("recording failed (exit %s)" % r.code, (r.stderr or "").strip()[-300:] or "the app printed no error", "doctor")
    calls = cassette.load(calls_path)
    if not calls and r.data.get("skipped"):
        raise UserError("none of the %d calls of %s could be recorded" % (r.data["skipped"], fid),
                        "their arguments, results or dependency answers can't be stored as JSON", "status")
    if not calls:
        raise UserError("%s wasn't called while the workload ran" % fid, "is the command the same as for scan?",
                        "scan -- " + scan.get("display_cmd", ""))
    snap = r.data.get("snapshot") or {}
    old = load_json(d / "meta.json", {}) or {}
    meta = {
        "id": fid, "lang": adapter.name, "module": fn["module"], "qualname": fn["qualname"], "name": fn["name"], "kind": fn["kind"],
        "file": rel_to_project(resolve_in_project(fn["file"], project), project), "first_line": fn["first_line"], "in_main": fn["in_main"],
        "cmd": portable_cmd(scan["cmd"], project), "display_cmd": scan.get("display_cmd", shlex.join(scan["cmd"])),
        "cwd": rel_to_project(scan_cwd, project),
        "env": env_diff(snap.get("env"), project, adapter.private_env),
        "globals": snap.get("globals") or {},
        **adapter.record_meta(fn, r.data, project),
        "budget_s": budget or old.get("budget_s") or DEFAULT_BUDGET_S,
        "recorded": len(calls), "seen": r.data.get("seen"), "skipped": r.data.get("skipped"),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "runtime": adapter.runtime_label(r.data),
    }
    save_local(project, executable=r.data.get("executable"))
    # env and globals are replayed as recorded (project paths in them are already relocatable)
    save_json(d / "meta.json", {**scrub(meta, project), "env": meta["env"], "globals": meta["globals"]})
    hp = d / adapter.hardened_file(meta)
    kept = hp.exists() and not force
    if not kept:
        hp.write_text(adapter.stub(fn, project))
    (d / "TASK.md").write_text(task_md(adapter, fn, meta, calls, scan, scan_root, project))
    nio = sum(len(c["io"]) for c in calls)
    if not quiet:
        print("  %s recorded %d calls (of %d seen) with %d dependency exchanges %s %s" % (
            ui.good(ui.OK), len(calls), meta["seen"] or len(calls), nio, ui.DOT,
            ui.dim("calls.json %.0f KB" % (calls_path.stat().st_size / 1024))))
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
