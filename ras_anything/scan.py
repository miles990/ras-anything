"""scan: run the workload clean and under seeded chaos, rank functions by risk × exposure × hardenability."""

from __future__ import annotations

import hashlib
import os
import shlex
import time
from pathlib import Path

from . import static, ui
from .paths import UserError, portable_cmd, ras_dir, rel_to_project, save_json, save_local, scrub
from .runner import project_root_for, python_cmd, run

W_DYNAMIC = 0.6  # measured chaos failures weigh more than static smells


def classify_run(r, clean_stdout: str) -> str:
    if r.hang:
        return "hang"
    if r.code != 0:
        return "crash"
    if r.stdout != clean_stdout:
        return "wrong"
    return "ok"


def func_id(module: str, qualname: str, file: str) -> str:
    mod = Path(file).stem if module in ("__main__", "") else module
    return "%s.%s" % (mod, qualname)


def hardenability(fn: dict, fi) -> str | None:
    """None if hardenable, else the reason it isn't (yet)."""
    if fn["kind"] == "nested":
        return "nested function (v0.1: top-level functions only)"
    if fn["kind"] in ("method", "classmethod"):
        return "%s of a class (v0.1: top-level functions and @staticmethod)" % fn["kind"].replace("classmethod", "classmethod")
    if fn.get("async") or fn.get("gen"):
        return "async/generator function (not supported yet)"
    if fn["in_main"] and not (fi and fi.has_main_guard):
        return "in the entry script with no `if __name__ == \"__main__\"` guard"
    if fn.get("arg_ok") is False:
        return "arguments can't be stored as JSON, so calls can't be recorded"
    if fn.get("ret_ok") is False:
        return "return value can't be stored as JSON, so results can't be compared"
    if not fn["boundary"]:
        return "makes no dependency calls"
    return None


def scan(cmd: list[str], seeds: int = 5, rate: float = 0.25, cwd: Path | None = None, quiet: bool = False) -> dict:
    cwd = Path(cwd or os.getcwd()).resolve()
    stored = portable_cmd(cmd, cwd)
    shown = shlex.join(stored)
    typed = list(cmd)
    cmd = python_cmd(cmd)
    root = project_root_for(cmd, cwd)
    t0 = time.perf_counter()
    if not quiet:
        ui.headline("scan", "%s %s 1 clean + %d chaos runs" % (shown, ui.DOT, seeds))
    clean = run(cmd, {"mode": "scan"}, cwd, root=root)
    if clean.code != 0 or clean.data is None:
        tail = (clean.stderr or "").strip().splitlines()[-3:]
        raise UserError("the clean run failed (exit %s)%s" % (clean.code, (": " + tail[-1]) if tail else ""),
                        "scan needs a workload that succeeds without faults" + (
                            ": " + " | ".join(tail[:-1]) if len(tail) > 1 else " (the command printed no error)"),
                        "doctor")
    runs = []
    per_fn: dict = {}
    for seed in range(1, seeds + 1):
        r = run(cmd, {"mode": "chaos", "seed": seed, "rate": rate, "outage_rate": 0.05}, cwd, root=root, timeout=120)
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
        fi = static.load(f["file"])
        info = fi.functions.get(f["line"]) if fi else None
        if info is None:
            continue
        a = fi.analyze(info)
        in_main = f["module"] == "__main__"
        d = {
            "id": func_id(f["module"], info["qualname"], f["file"]),
            "module": f["module"] if not in_main else Path(f["file"]).stem,
            "qualname": info["qualname"], "name": f["name"], "kind": info["kind"], "async": info["async"],
            "file": rel_to_project(f["file"], cwd), "line": info["line"], "first_line": f["line"], "in_main": in_main,
            "calls": f["calls"], "boundary": f.get("boundary", 0), "boundary_kinds": f.get("kinds", {}),
            "gen": f.get("gen"), "arg_ok": f.get("arg_ok"), "ret_ok": f.get("ret_ok"),
            "signals": a["signals"], "static_risk": a["static_risk"],
        }
        dyn = per_fn.get((f["file"], f["line"], f["name"]))
        d["chaos"] = dyn
        d["dynamic_risk"] = round(dyn["failed"] / dyn["runs"], 4) if dyn and dyn["runs"] else None
        risk = d["static_risk"] if d["dynamic_risk"] is None else \
            (1 - W_DYNAMIC) * d["static_risk"] + W_DYNAMIC * d["dynamic_risk"]
        d["risk"] = round(risk, 4)
        d["exposure"] = round(d["boundary"] / total, 4) if total else 0.0
        reason = hardenability(d, fi)
        d["hardenable"] = reason is None
        d["verdict"] = "harden it" if reason is None else reason
        d["score"] = round(100 * d["risk"] * d["exposure"] * (1.0 if reason is None else 0.0), 1)
        d["why"] = why(d)
        functions.append(d)

    functions.sort(key=lambda d: (-d["score"], -(d["risk"] * d["exposure"]), -d["boundary"], d["id"]))
    result = {
        "version": 1,
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
    (ras_dir(cwd) / "clean.stdout").write_text(clean.stdout)
    if not quiet:
        print_scan(result)
    return result


def why(d: dict) -> str:
    parts = [txt for k, txt in sorted(d["signals"].items(), key=lambda kv: -static.SIGNALS[kv[0]][0])][:3]
    dyn = d.get("chaos")
    if dyn and dyn["runs"]:
        bad = {k: v for k, v in dyn["outcomes"].items() if k != "ok"}
        if bad:
            parts.insert(0, "app failed in %d/%d chaos runs that hit it (%s)" % (
                dyn["failed"], dyn["runs"], ", ".join("%s %d" % kv for kv in sorted(bad.items()))))
        else:
            parts.insert(0, "app survived all %d chaos runs that hit it" % dyn["runs"])
    return " · ".join(parts)


def print_scan(result: dict, limit: int = 10) -> None:
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
        print("  " + ui.warn("no function in your project makes dependency calls (urlopen, subprocess.run, requests)"))
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
