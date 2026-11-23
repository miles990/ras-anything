"""bench: whole-app runs under live seeded chaos, original vs hardened, interleaved."""

from __future__ import annotations

import os
import platform
import shlex
import time

from . import ax, diagnose, faults, lang, slo, ui
from .judge import classify_run, duplicates
from .paths import UserError, find_project, harden_dir, load_json, portable_cmd, resolve_in_project, runnable_cmd, save_json, scrub
from .runner import run


def _places(rs) -> dict:
    out: dict = {}
    for r in rs:
        u = r.get("uncaught")
        if r["outcome"] in ("crash", "hang") and u:
            k = "%s in %s" % ("hang" if u.get("hang") else u["type"], u.get("where") or "?")
            out[k] = out.get(k, 0) + 1
    return out


def _x(v) -> str:
    return "—" if v is None else "%.2f" % v


def _amplification(stats) -> dict:
    """Dependency attempts per call of the enabled functions, over all calls and over those that met an outage."""
    stats = list(stats)
    calls, hit = sum(s["calls"] for s in stats), sum(s.get("outage_calls", 0) for s in stats)
    return {"amplification": sum(s.get("attempts", 0) for s in stats) / calls if calls else None,
            "outage_amplification": sum(s.get("outage_attempts", 0) for s in stats) / hit if hit else None, "outage_calls": hit}


def _pctl(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def bench(cmd: list[str] | None = None, seeds: int = 20, rate: float = 0.1, markers: list[str] | None = None,
          slo_spec: str | None = None, quiet: bool = False) -> dict:
    project = find_project()
    scan = load_json(project / ".ras" / "scan.json") or {}
    stored = portable_cmd(cmd or scan.get("cmd") or [], project)
    shown = shlex.join(stored)
    typed = cmd or runnable_cmd(scan.get("cmd") or [], project)
    if not typed:
        raise UserError("no command to bench", "pass it after -- or run scan first", "bench -- python app.py")
    adapter = lang.detect(typed)
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
        targets.append({"id": fid, "module": m["module"], "qualname": m["qualname"], "in_main": m["in_main"],
                        "file": str(resolve_in_project(m["file"], project))})
    cwd = resolve_in_project(scan.get("cwd") or ".", project) if scan.get("cwd") else project
    cmd = adapter.resolve(typed, cwd)
    root = resolve_in_project(scan.get("root") or ".", project) if scan.get("root") else adapter.project_root(cmd, cwd)
    base_cfg = {"mode": "bench", "rate": rate, "outage_rate": 0.05, "scale": 0.05, "hang_s": 2.0, "targets": targets}
    inc_env = os.environ.get("RAS_ANYTHING_INCIDENTS", "off")
    variants = {"original": {"RAS_ANYTHING_DISABLE": "1", "RAS_ANYTHING_INCIDENTS": inc_env}, "hardened": {"RAS_ANYTHING_DISABLE": "0", "RAS_ANYTHING_INCIDENTS": inc_env}}
    if not quiet:
        ui.headline("bench", "%s %s %d seeds × original/hardened, interleaved %s live chaos %s per attempt" % (
            shown, ui.DOT, seeds, ui.DOT, ui.pct(rate, 0)))
    clean = {}
    for v, env in variants.items():
        r = run(adapter, cmd, dict(base_cfg, seed=None), cwd, env_extra=env, root=root, timeout=300)
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
    degraded_stdouts: set[str] = set()
    raw_runs = []
    first_wrong_diff = None
    for seed in range(1, seeds + 1):
        order = list(variants) if seed % 2 else list(reversed(list(variants)))
        line = {}
        for v in order:
            r = run(adapter, cmd, dict(base_cfg, seed=seed), cwd, env_extra=variants[v], root=root, timeout=180)
            d = r.data or {}
            att = d.get("attempts") or []
            fbs = sum(st.get("fallback", 0) for st in (d.get("stats") or {}).values())
            outc = classify_run(r, baseline, markers, fbs, sum(1 for a in att if a.get("failed")))
            if v == "hardened" and outc == "degraded" and r.code == 0:
                degraded_stdouts.add(r.stdout)
            if v == "hardened" and outc == "wrong" and first_wrong_diff is None:
                first_wrong_diff = {"seed": seed, "expected": baseline[:240], "actual": r.stdout[:240],
                                    "faults": [a.get("fault") for a in att if a.get("fault")]}
            rec = {"seed": seed, "outcome": outc, "exit": r.code, "wall_s": round(r.wall, 4),
                   "fallbacks": fbs,
                   "faults": sum(1 for a in att if a.get("fault")), "attempts": len(att), "dups": duplicates(att),
                   "uncaught": d.get("uncaught")}
            raw_runs.append((v, rec, r.stdout, r.stderr or ""))
            runs[v].append(rec)
            for fid, st in (d.get("stats") or {}).items():
                agg = stats[v].setdefault(fid, {"calls": 0, "ok": 0, "fallback": 0, "raised": 0, "attempts": 0, "outage_calls": 0,
                                                "outage_attempts": 0, "p99": []})
                agg["calls"] += st["calls"]
                agg["ok"] += st["ok"]
                agg["fallback"] += st["fallback"]
                agg["raised"] += sum(st["raised"].values())
                for k in ("attempts", "outage_calls", "outage_attempts"):
                    agg[k] += st.get(k, 0)
                if st.get("p99") is not None:
                    agg["p99"].append(st["p99"])
            line[v] = rec
        if not quiet:
            def cell(rec):
                o = rec["outcome"]
                m = ui.good(ui.OK) if o in ("correct", "degraded") else ui.bad(ui.FAIL)
                return "%s %-8s %6s" % (m, o, ui.secs(rec["wall_s"]))
            print("  seed %2d   original %s %s   hardened %s %s %s" % (
                  seed, cell(line["original"]), ui.dim("%2d fault%s" % (line["original"]["faults"], " " if line["original"]["faults"] == 1 else "s")),
                  cell(line["hardened"]), ui.dim("%2d fault%s" % (line["hardened"]["faults"], " " if line["hardened"]["faults"] == 1 else "s")),
                  ui.warn(" dup %d→%d" % (line["original"]["dups"], line["hardened"]["dups"]))
                  if line["original"]["dups"] or line["hardened"]["dups"] else ""), flush=True)
    for v, rec, stdout, _stderr in raw_runs:
        if v == "original" and rec["outcome"] == "wrong" and rec["exit"] == 0 and stdout in degraded_stdouts:
            rec["outcome"] = "degraded"
    summary = {}
    for v, rs in runs.items():
        n = len(rs)
        c = {k: sum(1 for r in rs if r["outcome"] == k) for k in ("correct", "degraded", "wrong", "crash", "hang")}
        walls = [r["wall_s"] for r in rs]
        summary[v] = {"runs": n, **c, "availability": (c["correct"] + c["degraded"]) / n if n else 0.0,
                      "crash_rate": c["crash"] / n if n else 0.0, "hang_rate": c["hang"] / n if n else 0.0,
                      "wrong_rate": c["wrong"] / n if n else 0.0,
                      "p50_wall": _pctl(walls, 0.5), "p99_wall": _pctl(walls, 0.99),
                      "identical_rate": c["correct"] / n if n else 0.0,
                      "dups": sum(r["dups"] for r in rs), "faults": sum(r["faults"] for r in rs),
                      "failures_by_place": _places(rs), **_amplification(stats[v].values()),
                      "clean_per_call": _amplification((clean[v].data or {}).get("stats", {}).values())["amplification"],
                      "clean_wall": round(clean[v].wall, 4)}
    for v in stats:
        for st in stats[v].values():
            st["p99"] = max(st["p99"]) if st["p99"] else None
    srv_bench = {}
    bench_leaks = []
    for v in variants:
        diags = [diagnose.diagnose(err) for vv, rec, _out, err in raw_runs
                 if vv == v and (rec["faults"] > 0 or rec["outcome"] != "correct")]
        if v == "hardened":
            for vv, _rec, _out, err in raw_runs:
                if vv == "hardened" and err:
                    bench_leaks.extend(ax.scan_leaks(err))
        n_d = len(diags)
        det = sum(1 for d in diags if d.get("detected"))
        lines_list = sorted(d["lines_read"] for d in diags if d.get("lines_read") is not None)
        ttd_list = sorted(d["ttd_ms"] for d in diags if d.get("ttd_ms") is not None)
        srv_bench[v] = {
            "cases": n_d,
            "detected": det,
            "detection_rate": (det / n_d) if n_d else None,
            "lines_to_diagnosis_p50": lines_list[len(lines_list) // 2] if lines_list else None,
            "ttd_ms_p50": ttd_list[len(ttd_list) // 2] if ttd_list else None,
        }
    res = {
        "lang": adapter.name, "cmd": stored, "display_cmd": shown, "ids": ids, "seeds": seeds, "rate": rate, "outage_rate": 0.05,
        "time_scale": 0.05, "hang_cap_s": 2.0, "degraded_markers": markers, "clean_outputs_equal": same_clean,
        "summary": summary, "runs": runs, "functions": stats, "first_wrong": first_wrong_diff,
        "serviceability": srv_bench, "leaks": sorted(set(bench_leaks)),
        "host": {"machine": platform.machine(), "system": platform.system(), "release": platform.release(),
                 "python": platform.python_version(),
                 "runtime": adapter.runtime_label(clean["original"].data), "cpus": os.cpu_count(), "node": platform.node()[:0] or None},
        "fault_model": {k: faults.DESCRIBE[k] for k in faults.KINDS},
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    res = scrub(res, project)
    slo_res = slo.evaluate(project=project, bench=res, targets=slo_spec, save=True)
    res["slo"] = slo_res
    save_json(project / ".ras" / "bench.json", res)
    from . import export as _export
    _export.emit_event(project, "bench.completed", {
        "seeds": seeds,
        "availability_original": ((res.get("summary") or {}).get("original") or {}).get("availability"),
        "availability_hardened": ((res.get("summary") or {}).get("hardened") or {}).get("availability"),
    })
    _export.sync_out(project)
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
         ui.bold(ui.pct(h["availability"])), ui.dim("exit 0, output correct or declared-degraded")],
        ["  identical output", ui.pct(o["identical_rate"]), arrow(o["identical_rate"], h["identical_rate"], True) + " " +
         ui.pct(h["identical_rate"]), ui.dim("strict: byte-identical to the clean run")],
        ["crashes", ui.pct(o["crash_rate"]), arrow(o["crash_rate"], h["crash_rate"], False) + " " + ui.pct(h["crash_rate"]), ""],
        ["hangs", ui.pct(o["hang_rate"]), arrow(o["hang_rate"], h["hang_rate"], False) + " " + ui.pct(h["hang_rate"]),
         ui.dim("a call with no timeout blocked (cap %gs real)" % res["hang_cap_s"])],
        ["wrong output", ui.pct(o["wrong_rate"]), arrow(o["wrong_rate"], h["wrong_rate"], False) + " " + ui.pct(h["wrong_rate"]), ""],
        ["duplicate side effects", str(o["dups"]), arrow(o["dups"], h["dups"], False) + " " + str(h["dups"]),
         ui.dim("non-idempotent requests that reached the server twice")],
        ["attempts per call", _x(o["amplification"]), _x(h["amplification"]),
         ui.dim("retry amplification, enabled functions (no faults: %s)" % _x(h["clean_per_call"]))],
        ["  in an outage", _x(o["outage_amplification"]), _x(h["outage_amplification"]),
         ui.dim("calls that met one: %d / %d" % (o["outage_calls"], h["outage_calls"]))],
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
    for v in ("original", "hardened"):
        places = res["summary"][v].get("failures_by_place") or {}
        if places:
            print(ui.dim("  %s failures: " % v) + ", ".join("%s ×%d" % kv for kv in sorted(places.items(), key=lambda kv: -kv[1])))
    print()
    print(ui.dim("  live chaos at the boundary, real upstream behind it · injected waits ×%g · seeds 1–%d, same per pair · saved .ras/bench.json"
                 % (res["time_scale"], res["seeds"])))
    if res.get("first_wrong"):
        fw = res["first_wrong"]
        print(ui.warn("  first hardened wrong output (seed %s, faults=%s): %r (expected %r)" % (
            fw["seed"], fw["faults"], fw["actual"][:120], fw["expected"][:120])))
    if res.get("slo"):
        slo.print_slo(res["slo"])
    if not res["clean_outputs_equal"]:
        print(ui.bad("  ✗ the hardened app prints different output with no faults: fix before shipping"))
    ui.next_hint("report")
