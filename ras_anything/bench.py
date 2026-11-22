"""bench: whole-app runs under live seeded chaos, original vs hardened, interleaved."""

from __future__ import annotations

import os
import platform
import shlex
import time
from pathlib import Path

from . import faults, ui
from .paths import UserError, find_project, harden_dir, load_json, save_json
from .runner import python_cmd, run, project_root_for


def duplicates(attempts: list) -> int:
    groups: dict = {}
    for n, a in enumerate(attempts or []):
        if a.get("nonidempotent") and a.get("reached"):
            groups.setdefault(a["key"], set()).add(a.get("idem") or "<no key #%d>" % n)
    return sum(len(g) - 1 for g in groups.values())


def classify(r, baseline: str, markers: list[str]) -> str:
    if r.hang:
        return "hang"
    if r.code != 0:
        return "crash"
    if r.stdout == baseline:
        return "correct"
    a, b = r.stdout.splitlines(), baseline.splitlines()
    if markers and len(a) == len(b):
        diff = [x for x, y in zip(a, b) if x != y]
        if all(any(m in x for m in markers) for x in diff):
            return "degraded"
    return "wrong"


def _pctl(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def bench(cmd: list[str] | None = None, seeds: int = 20, rate: float = 0.1, markers: list[str] | None = None,
          quiet: bool = False) -> dict:
    project = find_project()
    scan = load_json(project / ".ras" / "scan.json") or {}
    shown = shlex.join(cmd) if cmd else scan.get("display_cmd")
    cmd = python_cmd(cmd) if cmd else scan.get("cmd")
    if not cmd:
        raise UserError("no command to bench", "pass it after -- or run scan first", "bench -- python app.py")
    state = load_json(project / ".ras" / "state.json", {"enabled": {}})
    ids = list(state["enabled"])
    if not ids:
        raise UserError("nothing is enabled", "bench compares the original against enabled hardened functions",
                        "enable <id>")
    old = load_json(project / ".ras" / "bench.json") or {}
    markers = markers if markers is not None else old.get("degraded_markers", [])
    targets = []
    for fid in ids:
        m = load_json(harden_dir(project, fid) / "meta.json")
        targets.append({"id": fid, "module": m["module"], "qualname": m["qualname"], "in_main": m["in_main"]})
    cwd = Path(scan.get("cwd") or os.getcwd())
    root = Path(scan.get("root") or project_root_for(cmd, cwd))
    base_cfg = {"mode": "bench", "rate": rate, "outage_rate": 0.05, "scale": 0.05, "hang_s": 2.0, "targets": targets}
    variants = {"original": {"RAS_ANYTHING_DISABLE": "1"}, "hardened": {"RAS_ANYTHING_DISABLE": "0"}}
    if not quiet:
        ui.headline("bench", "%s %s %d seeds × original/hardened, interleaved %s live chaos %s per attempt" % (
            shown, ui.DOT, seeds, ui.DOT, ui.pct(rate, 0)))
    clean = {}
    for v, env in variants.items():
        r = run(cmd, dict(base_cfg, seed=None), cwd, env_extra=env, root=root, timeout=300)
        clean[v] = r
        if r.code != 0:
            raise UserError("the clean %s run failed (exit %d)" % (v, r.code), (r.stderr or "").strip()[-300:], "doctor")
    baseline = clean["original"].stdout
    same_clean = clean["hardened"].stdout == baseline
    if not quiet:
        print("  %s clean runs %s" % (ui.good(ui.OK) if same_clean else ui.bad(ui.FAIL),
                                      "print identical output" if same_clean else "DIFFER: hardened changed the app's output"))
    runs = {v: [] for v in variants}
    stats = {v: {} for v in variants}
    for seed in range(1, seeds + 1):
        order = list(variants) if seed % 2 else list(reversed(list(variants)))
        line = {}
        for v in order:
            r = run(cmd, dict(base_cfg, seed=seed), cwd, env_extra=variants[v], root=root, timeout=180)
            d = r.data or {}
            att = d.get("attempts") or []
            rec = {"seed": seed, "outcome": classify(r, baseline, markers), "exit": r.code, "wall_s": round(r.wall, 4),
                   "faults": sum(1 for a in att if a.get("fault")), "attempts": len(att), "dups": duplicates(att),
                   "uncaught": d.get("uncaught")}
            runs[v].append(rec)
            for fid, st in (d.get("stats") or {}).items():
                agg = stats[v].setdefault(fid, {"calls": 0, "ok": 0, "fallback": 0, "raised": 0, "p99": []})
                agg["calls"] += st["calls"]
                agg["ok"] += st["ok"]
                agg["fallback"] += st["fallback"]
                agg["raised"] += sum(st["raised"].values())
                if st.get("p99") is not None:
                    agg["p99"].append(st["p99"])
            line[v] = rec
        if not quiet:
            def cell(rec):
                o = rec["outcome"]
                m = ui.good(ui.OK) if o in ("correct", "degraded") else ui.bad(ui.FAIL)
                return "%s %-8s %6s" % (m, o, ui.secs(rec["wall_s"]))
            print("  seed %2d  %s   original %s   hardened %s %s" % (seed, ui.dim("%2d faults" % line["original"]["faults"]),
                  cell(line["original"]), cell(line["hardened"]),
                  ui.warn(" %d dup" % line["original"]["dups"]) if line["original"]["dups"] else ""), flush=True)
    summary = {}
    for v, rs in runs.items():
        n = len(rs)
        c = {k: sum(1 for r in rs if r["outcome"] == k) for k in ("correct", "degraded", "wrong", "crash", "hang")}
        walls = [r["wall_s"] for r in rs]
        summary[v] = {"runs": n, **c, "availability": (c["correct"] + c["degraded"]) / n if n else 0.0,
                      "crash_rate": c["crash"] / n if n else 0.0, "hang_rate": c["hang"] / n if n else 0.0,
                      "wrong_rate": c["wrong"] / n if n else 0.0,
                      "p50_wall": _pctl(walls, 0.5), "p99_wall": _pctl(walls, 0.99),
                      "dups": sum(r["dups"] for r in rs), "faults": sum(r["faults"] for r in rs),
                      "clean_wall": round(clean[v].wall, 4)}
    for v in stats:
        for st in stats[v].values():
            st["p99"] = max(st["p99"]) if st["p99"] else None
    res = {
        "cmd": cmd, "display_cmd": shown, "ids": ids, "seeds": seeds, "rate": rate, "outage_rate": 0.05,
        "time_scale": 0.05, "hang_cap_s": 2.0, "degraded_markers": markers, "clean_outputs_equal": same_clean,
        "summary": summary, "runs": runs, "functions": stats,
        "host": {"machine": platform.machine(), "system": platform.system(), "release": platform.release(),
                 "python": platform.python_version(), "cpus": os.cpu_count(), "node": platform.node()[:0] or None},
        "fault_model": {k: faults.DESCRIBE[k] for k in faults.KINDS},
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    save_json(project / ".ras" / "bench.json", res)
    if not quiet:
        print_bench(res)
    return res


def print_bench(res: dict) -> None:
    o, h = res["summary"]["original"], res["summary"]["hardened"]

    def arrow(a, b, higher_better):
        if a == b or a is None or b is None:
            return " "
        good = (b > a) == higher_better
        sym = ui.UP if b > a else ui.DOWN
        return ui.good(sym) if good else ui.bad(sym)

    rows = [
        ["app availability", ui.pct(o["availability"]), arrow(o["availability"], h["availability"], True) + " " +
         ui.bold(ui.pct(h["availability"])), ui.dim("exit 0 and output correct%s" % (" or degraded" if res["degraded_markers"] else ""))],
        ["crashes", ui.pct(o["crash_rate"]), arrow(o["crash_rate"], h["crash_rate"], False) + " " + ui.pct(h["crash_rate"]), ""],
        ["hangs", ui.pct(o["hang_rate"]), arrow(o["hang_rate"], h["hang_rate"], False) + " " + ui.pct(h["hang_rate"]),
         ui.dim("a call with no timeout blocked (cap %gs real)" % res["hang_cap_s"])],
        ["wrong output", ui.pct(o["wrong_rate"]), arrow(o["wrong_rate"], h["wrong_rate"], False) + " " + ui.pct(h["wrong_rate"]), ""],
        ["duplicate side effects", str(o["dups"]), arrow(o["dups"], h["dups"], False) + " " + str(h["dups"]),
         ui.dim("non-idempotent requests that reached the server twice")],
        ["p50 wall", ui.secs(o["p50_wall"]), ui.secs(h["p50_wall"]), ""],
        ["p99 wall", ui.secs(o["p99_wall"]), arrow(o["p99_wall"], h["p99_wall"], False) + " " + ui.secs(h["p99_wall"]), ""],
        ["faults injected", str(o["faults"]), str(h["faults"]), ui.dim("hardened retries see more attempts")],
    ]
    print()
    print(ui.table(["%d runs each" % o["runs"], "original", "hardened", ""], rows, "lrrl"))
    if res["functions"].get("hardened"):
        print()
        frows = []
        for fid, st in res["functions"]["hardened"].items():
            ost = res["functions"]["original"].get(fid, {})
            frows.append([fid, str(st["calls"]), "%d → %d" % (ost.get("raised", 0), st["raised"]), str(st["fallback"]),
                          ui.secs(ost.get("p99")) + " → " + ui.secs(st["p99"])])
        print(ui.table(["function", "calls", "raised (orig → hard)", "fallbacks", "worst p99/run"], frows, "lrrrl"))
    print()
    print(ui.dim("  live chaos at the boundary, real upstream behind it · injected waits ×%g · seeds 1–%d, same per pair · saved .ras/bench.json"
                 % (res["time_scale"], res["seeds"])))
    if not res["clean_outputs_equal"]:
        print(ui.bad("  ✗ the hardened app prints different output with no faults: fix before shipping"))
    ui.next_hint("report")
