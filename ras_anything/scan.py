"""scan: run the workload clean and under seeded chaos, rank functions by risk × exposure × hardenability."""

from __future__ import annotations

import hashlib
import os
import shlex
import time
from pathlib import Path

from . import lang, signals, ui
from .paths import UserError, portable_cmd, ras_dir, rel_to_project, save_json, save_local, scrub
from .runner import run

W_DYNAMIC = 0.6  # measured chaos failures weigh more than static smells


def classify_run(r, clean_stdout: str) -> str:
    if r.hang:
        return "hang"
    if r.code != 0:
        return "crash"
    if r.stdout != clean_stdout:
        return "wrong"
    return "ok"


def scan(cmd: list[str], seeds: int = 5, rate: float = 0.25, cwd: Path | None = None, quiet: bool = False) -> dict:
    cwd = Path(cwd or os.getcwd()).resolve()
    adapter = lang.detect(cmd)
    stored = portable_cmd(cmd, cwd)
    shown = shlex.join(stored)
    typed = list(cmd)
    cmd = adapter.resolve(cmd, cwd)
    root = adapter.project_root(cmd, cwd)
    t0 = time.perf_counter()
    if not quiet:
        ui.headline("scan", "%s %s 1 clean + %d chaos runs" % (shown, ui.DOT, seeds))
    clean = run(adapter, cmd, {"mode": "scan"}, cwd, root=root)
    if clean.code != 0:
        tail = (clean.stderr or "").strip().splitlines()[-3:]
        raise UserError("the clean run failed (exit %s)%s" % (clean.code, (": " + tail[-1]) if tail else ""),
                        "scan needs a workload that succeeds without faults" + (
                            ": " + " | ".join(tail[:-1]) if len(tail) > 1 else " (the command printed no error)"),
                        "doctor")
    if clean.data is None:
        raise UserError("the app ran, but ras-anything couldn't watch it", "its %s agent never loaded: is the app started through "
                        "a wrapper, or with flags that ignore the environment?" % adapter.label, "scan -- " + adapter.example)
    runs = []
    per_fn: dict = {}
    for seed in range(1, seeds + 1):
        r = run(adapter, cmd, {"mode": "chaos", "seed": seed, "rate": rate, "outage_rate": 0.05}, cwd, root=root, timeout=120)
        outcome = classify_run(r, clean.stdout)
        hit = set()
        nfaults = 0
        for f in (r.data or {}).get("functions", []):
            if f.get("faults"):
                key = (f["file"], f["line"], f["name"])
                hit.add(key)
                nfaults += sum(f["faults"].values())
        for key in hit:
            s = per_fn.setdefault(key, {"runs": 0, "failed": 0, "outcomes": {}})
            s["runs"] += 1
            s["failed"] += outcome != "ok"
            s["outcomes"][outcome] = s["outcomes"].get(outcome, 0) + 1
        runs.append({"seed": seed, "outcome": outcome, "exit": r.code, "wall_s": round(r.wall, 3), "faults": nfaults,
                     "uncaught": (r.data or {}).get("uncaught")})
        if not quiet:
            mark = ui.good(ui.OK) if outcome == "ok" else ui.bad(ui.FAIL)
            print("  %s chaos seed %-2d %-5s %s" % (mark, seed, outcome, ui.dim("%d fault%s injected" % (nfaults, "" if nfaults == 1 else "s"))), flush=True)

    total = clean.data.get("boundary_total") or 0
    functions = []
    for f in clean.data["functions"]:
        facts = adapter.describe(f)
        if facts is None:
            continue
        d = {
            **facts, "file": rel_to_project(f["file"], cwd), "first_line": f["line"],
            "calls": f["calls"], "boundary": f.get("boundary", 0), "boundary_kinds": f.get("kinds", {}),
            "gen": f.get("gen"), "arg_ok": f.get("arg_ok"), "ret_ok": f.get("ret_ok"),
        }
        dyn = per_fn.get((f["file"], f["line"], f["name"]))
        d["chaos"] = dyn
        d["dynamic_risk"] = round(dyn["failed"] / dyn["runs"], 4) if dyn and dyn["runs"] else None
        risk = d["static_risk"] if d["dynamic_risk"] is None else \
            (1 - W_DYNAMIC) * d["static_risk"] + W_DYNAMIC * d["dynamic_risk"]
        d["risk"] = round(risk, 4)
        d["exposure"] = round(d["boundary"] / total, 4) if total else 0.0
        reason = adapter.hardenability(d)
        d["hardenable"] = reason is None
        d["verdict"] = "harden it" if reason is None else reason
        d["score"] = round(100 * d["risk"] * d["exposure"] * (1.0 if reason is None else 0.0), 1)
        d["why"] = why(d)
        functions.append(d)

    functions.sort(key=lambda d: (-d["score"], -(d["risk"] * d["exposure"]), -d["boundary"], d["id"]))
    result = {
        "version": 1, "lang": adapter.name,
        "cmd": stored, "display_cmd": shown, "cwd": ".", "root": rel_to_project(root, cwd),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "clean": {"wall_s": round(clean.wall, 3), "stdout_sha256": hashlib.sha256(clean.stdout.encode()).hexdigest(),
                  "boundary_calls": total},
        "chaos": {"seeds": seeds, "rate": rate, "outage_rate": 0.05, "runs": runs},
        "functions": functions,
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }
    result = scrub(result, cwd)
    save_local(cwd, cmd=typed, portable=stored, executable=clean.data.get("executable"))
    save_json(ras_dir(cwd) / "scan.json", result)
    from . import export as _export
    _export.emit_event(cwd, "scan.completed", {"functions": len(functions), "lang": adapter.name})
    _export.sync_out(cwd)
    (ras_dir(cwd) / "clean.stdout").write_text(clean.stdout)
    if not quiet:
        print_scan(result, adapter)
    return result


def why(d: dict) -> str:
    parts = signals.by_weight(d["signals"])[:3]
    dyn = d.get("chaos")
    if dyn and dyn["runs"]:
        bad = {k: v for k, v in dyn["outcomes"].items() if k != "ok"}
        if bad:
            parts.insert(0, "app failed in %d/%d chaos runs that hit it (%s)" % (
                dyn["failed"], dyn["runs"], ", ".join("%s %d" % kv for kv in sorted(bad.items()))))
        else:
            parts.insert(0, "app survived all %d chaos runs that hit it" % dyn["runs"])
    return " · ".join(parts)


def print_scan(result: dict, adapter, limit: int = 10) -> None:
    fns = [d for d in result["functions"] if d["boundary"] or d["signals"]]
    runs = result["chaos"]["runs"]
    failed = sum(r["outcome"] != "ok" for r in runs)
    print()
    print("  " + ui.bold("app under chaos: %d/%d runs failed" % (failed, len(runs))) + ui.dim(
        "  (%s per boundary attempt %s seeds 1–%d %s clean run %s, %d dependency calls)" % (
            ui.pct(result["chaos"]["rate"], 0), ui.DOT, len(runs), ui.DOT, ui.secs(result["clean"]["wall_s"]),
            result["clean"]["boundary_calls"])))
    print()
    rows, subs = [], []
    for i, d in enumerate(fns[:limit], 1):
        verdict = ui.good(ui.OK + " harden it") if d["hardenable"] else ui.dim(ui.FAIL + " " + d["verdict"])
        dyn = d.get("chaos")
        rows.append([str(i), ui.bold(d["id"]) if d["hardenable"] else d["id"], str(d["calls"]), str(d["boundary"]),
                     "%.2f" % d["risk"], ui.pct(d["exposure"], 0),
                     ("%d/%d" % (dyn["failed"], dyn["runs"])) if dyn else "—",
                     ui.bold("%.1f" % d["score"]) if d["score"] else ui.dim("0"), verdict])
        subs.append(d["why"])
    if not rows:
        print("  " + ui.warn("no function in your project makes dependency calls (%s)" % adapter.boundaries))
        return
    print(ui.table(["#", "function", "calls", "dep", "risk", "exposure", "chaos fail", "score", "verdict"], rows,
                   "rlrrrrrrl", sub=subs))
    hidden = len(result["functions"]) - len(fns)
    print()
    print(ui.dim("  score = risk × exposure × hardenable · risk = 0.4 static + 0.6 measured under chaos"))
    print(ui.dim("  exposure = share of the app's dependency calls" + (
        " · %d functions without dependency calls not shown" % hidden if hidden else "")))
    print(ui.dim("  saved .ras/scan.json"))
    top = next((d for d in fns if d["hardenable"]), None)
    if top:
        ui.next_hint("harden %s --agent claude" % top["id"])
    else:
        ui.next_hint("doctor")
