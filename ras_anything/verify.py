"""verify: prove the hardened function keeps the happy path and is more available under faults.

This decides which trials to run and what they mean; a replay worker in the app's own language runs
them (docs/ARCHITECTURE.md, "Replay worker").
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from . import ai, ax, cassette, diagnose, faults, judge, lang, ui
from .paths import UserError, find_project, harden_dir, load_json, save_json, scrub, sha256_file

CLASSES = ("ok", "degraded", "error", "wrong", "hang")
TIMEOUT_S = 900


def resolve_id(project: Path, fid: str | None) -> str:
    base = project / ".ras" / "hardens"
    have = sorted(p.name for p in base.iterdir() if (p / "meta.json").exists()) if base.is_dir() else []
    if fid:
        if fid not in have:
            raise UserError("%s hasn't been hardened yet" % fid, "known: " + (", ".join(have) or "none"),
                            "harden %s" % fid)
        return fid
    if not have:
        raise UserError("nothing hardened yet", "verify checks the hardened code in .ras/hardens/<id>/", "harden")
    if len(have) > 1:
        def changed(h):
            a = lang.by_name((load_json(base / h / "meta.json") or {}).get("lang"))
            f = base / h / a.hardened_file(load_json(base / h / "meta.json") or {})
            return f.stat().st_mtime if f.exists() else 0
        return max(have, key=changed)
    return have[0]


class ReplayFailed(Exception):
    def __init__(self, fatal: dict):
        super().__init__(fatal["message"])
        self.fatal = fatal


def replay(adapter, project: Path, d: Path, meta: dict, trials: list[dict], deadline: float) -> dict:
    """Run trials in one worker process: [{"call", "side", "plan", "seed"}] -> one raw result each."""
    with tempfile.TemporaryDirectory(prefix="ras-verify-") as tmp:
        job, out = Path(tmp) / "job.json", Path(tmp) / "out.json"
        job.write_text(json.dumps({
            "protocol": 1, "lang": adapter.name, "project": str(project), "meta": meta, "cassette": str(d / "calls.json"),
            "hardened": str(d / adapter.hardened_file(meta)), "dependencies": meta.get("dependencies") or [], "trials": trials, "out": str(out), "constants": faults.CONSTANTS}))
        argv, env = adapter.worker_cmd(adapter.interpreter(project, meta), job)
        try:
            p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=max(1.0, deadline - time.perf_counter()))
        except subprocess.TimeoutExpired:
            raise ReplayFailed({"stage": "replay", "message": "verify ran for %d minutes without finishing: a busy loop that "
                                "never calls the dependency or sleeps?" % (TIMEOUT_S // 60)})
        except OSError as e:
            raise ReplayFailed({"stage": "start the replay worker", "message": "%s: %s" % (argv[0], e.strerror)})
        try:
            res = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ReplayFailed({"stage": "replay", "message": (p.stderr or "").strip()[-1500:] or
                                "the worker ended (exit %d) without an answer" % p.returncode})
    if res.get("fatal"):
        raise ReplayFailed(res["fatal"])
    if res.get("protocol") != 1 or len(res.get("results") or []) != len(trials):
        raise ReplayFailed({"stage": "replay", "message": "the %s worker's answer doesn't match the protocol" % adapter.label})
    return res


NONE = {"type": "none"}


def chaos_plan(calls: list[dict], keys: list[list[str]], usable: list[int], max_chaos: int, has_ai: bool = False) -> list[dict]:
    """The fault matrix and the seeded mixes over every usable call; then, for calls that make several
    distinct requests (a GET, then a POST), every fault on each request alone."""
    idx = usable[:max_chaos]
    out = []

    def add(i, row, label, plan, seed):
        out.append({"call": i, "row": row, "scenario": label, "plan": plan, "seed": int(seed * 2 ** 32)})

    matrix = faults.MATRIX + (ai.AI_FAULTS if has_ai else ())
    for name in matrix:
        for i in idx:
            add(i, name, name, {"type": "first", "fault": name, "key": None}, faults._u(name, None, None, i))
    for s in (1, 2, 3):
        for i in idx:
            add(i, "mixed", "mixed seed %d" % s,
                {"type": "seeded", "seed": s, "rate": 0.3, "outage_rate": 0.0, "salt": "call%d" % i}, faults._u("mixed", None, s, i))
    for i in idx:
        if len(keys[i]) < 2:
            continue
        for key in keys[i]:
            for name in faults.MATRIX:
                add(i, "%s (per-key)" % name, "%s on %s" % (name, key), {"type": "first", "fault": name, "key": key},
                    faults._u(name, key, None, i))
    return out


OUTAGE = {"healthy": (10, 0.5), "down": (40, 0.5), "recovered": (90, 1.0)}  # phase: calls, seconds between them


def outage_trials(usable: list[int], side: str) -> tuple[list[str], list[dict]]:
    """The recorded calls one after the other, while the dependency is fine, then down, then back (for longer than
    judge.KNOWN_DOWN_S). Nothing is reset in between: here breakers, budgets and caches show what they do."""
    phases = [p for p, (n, _) in OUTAGE.items() for _ in range(n)]
    return phases, [{"call": usable[k % len(usable)], "side": side, "plan": {"type": "down"} if p == "down" else NONE,
                     "seed": k, "gap_s": OUTAGE[p][1]} for k, p in enumerate(phases)]


def measure_outage(adapter, project, d, meta, calls, keys, usable, deadline) -> dict | None:
    if not usable:
        return None
    budget, out = float(meta["budget_s"]), {p: {"calls": n, "gap_s": gap} for p, (n, gap) in OUTAGE.items()}
    for side in ("original", "hardened"):  # each in a process of its own
        phases, trials = outage_trials(usable, side)
        raw = replay(adapter, project, d, meta, trials, deadline)["results"]
        ts = judge.classify_sequence(raw, [calls[t["call"]] for t in trials], [keys[t["call"]] for t in trials], budget,
                                     side == "hardened", [t["gap_s"] for t in trials])
        out[side] = judge.summarize_outage(phases, ts)
        bad = next((i for i, t in enumerate(ts) if t["class"] in ("wrong", "miss", "hang")), None)
        if side == "hardened" and bad is not None:
            out["first_failure"] = {"call": trials[bad]["call"], "args": _args(calls[trials[bad]["call"]]),
                                    "scenario": "sustained outage (%s)" % phases[bad], "original": None, "hardened": ts[bad]}
    return out


def measure(adapter, project: Path, d: Path, meta: dict, max_chaos: int) -> dict:
    budget = float(meta["budget_s"])
    try:
        calls = cassette.load(d / "calls.json")
    except (cassette.BadCassette, OSError) as e:
        raise ReplayFailed({"stage": "read calls.json", "message": "%s (record again: harden %s)" % (e, meta["id"])})
    keys = [judge.keys_of(c, project) for c in calls]
    deps = meta.get("dependencies") or cassette.dependencies(calls, project)
    meta = {**meta, "dependencies": deps}
    deadline = time.perf_counter() + TIMEOUT_S
    t0 = time.perf_counter()

    def pairs(specs):
        """Each spec run for both sides, original first; judged."""
        trials = [{"call": s["call"], "side": side, "plan": s["plan"], "seed": s["seed"]}
                  for s in specs for side in ("original", "hardened")]
        raw = replay(adapter, project, d, meta, trials, deadline)
        out = [judge.classify_trial(r, calls[t["call"]], keys[t["call"]], budget,
                                    fallback_allowed=t["side"] == "hardened" and t["plan"] is not NONE)
               for t, r in zip(trials, raw["results"])]
        return raw, list(zip(out[::2], out[1::2]))

    # (a) happy path ------------------------------------------------------------------------------
    happy = {"total": 0, "equal": 0, "original_nondeterministic": 0, "first_failure": None}
    usable = []
    raw, results = pairs([{"call": i, "plan": NONE, "seed": i} for i in range(len(calls))])
    for i, (o, h) in enumerate(results):
        if o["class"] != "ok":
            happy["original_nondeterministic"] += 1
            continue
        usable.append(i)
        happy["total"] += 1
        if h["class"] == "ok" and not judge.same_side_effects(o, h):
            h.update({"class": "wrong", "note": "left its arguments (or self) in a different state than the original"})
        if h["class"] == "ok":
            happy["equal"] += 1
        elif happy["first_failure"] is None:
            happy["first_failure"] = {"call": i, "args": _args(calls[i]), **h}

    has_ai = any(x.get("req", {}).get("ai") for c in calls for x in c.get("io", []))
    # (b) faults ----------------------------------------------------------------------------------
    specs = chaos_plan(calls, keys, usable, max_chaos, has_ai=has_ai)
    raw2, results = pairs(specs)
    sides = {"original": [o for o, _ in results], "hardened": [h for _, h in results]}
    rows: dict = {}
    first_bad = first_dup = None
    for s, (o, h) in zip(specs, results):
        row = rows.setdefault(s["row"], {"original": {}, "hardened": {}, "n": 0})
        row["n"] += 1
        for side, t in (("original", o), ("hardened", h)):
            row[side][t["class"]] = row[side].get(t["class"], 0) + 1
        seen = {"call": s["call"], "args": _args(calls[s["call"]]), "scenario": s["scenario"], "original": o, "hardened": h}
        if first_dup is None and (o["dups"] or h["dups"]):
            first_dup = seen
        if first_bad is None and (h["class"] in ("wrong", "hang", "miss") or h["dups"] > o["dups"]):
            first_bad = seen
    o, h = judge.summarize(sides["original"]), judge.summarize(sides["hardened"])
    crashes = judge.new_crashes(sides["original"], sides["hardened"], adapter.bug_exceptions)
    scenarios = list(dict.fromkeys(s["scenario"].replace(" seed ", ":") if s["row"] == "mixed" else
                                   ("per-key" if s["row"].endswith("(per-key)") else s["row"]) for s in specs))
    # (c) the dependency goes down and comes back, state kept -----------------------------------------
    outage = measure_outage(adapter, project, d, meta, calls, keys, usable, deadline)
    if outage:
        first_bad = first_bad or outage.pop("first_failure", None)
    # (d) V-8 serviceability + V-18 agent experience on faulted trials ----------------------------
    raw_o_list = raw2["results"][0::2]
    raw_h_list = raw2["results"][1::2]
    scored_o, scored_h = [], []
    orig_ax_cases, hard_ax_cases = [], []
    ax_leaks = []
    for s, ro, rh, jo, jh in zip(specs, raw_o_list, raw_h_list, sides["original"], sides["hardened"]):
        att_fail = next((a for a in (ro.get("attempts") or []) if a.get("fault")), None) or next(
            (a for a in (rh.get("attempts") or []) if a.get("fault")), None)
        t_fault = s["plan"].get("fault") or (att_fail.get("fault") if att_fail else None)
        t_dep = s["plan"].get("key") or (att_fail.get("key") if att_fail else None)
        if t_fault is not None or jo["class"] != "ok" or jh["class"] != "ok":
            do = diagnose.diagnose(ro.get("stderr") or "")
            dh = diagnose.diagnose(rh.get("stderr") or "")
            scored_o.append(diagnose.score_diagnosis(do, t_dep, t_fault))
            scored_h.append(diagnose.score_diagnosis(dh, t_dep, t_fault))
        if ro.get("outcome") in ("raise", "hang"):
            orig_ax_cases.append({"surface": ro.get("problem") or ro.get("exc") or (ro.get("stderr") or ""),
                                  "expected": ax.expected_action(diagnose._norm_fault(t_fault) if t_fault else None)})
        if rh.get("problem"):
            ax_leaks.extend(ax.scan_leaks(rh["problem"]))
            hard_ax_cases.append({"surface": rh["problem"],
                                  "expected": ax.expected_action(diagnose._norm_fault(t_fault) if t_fault else None)})
        elif rh.get("outcome") in ("raise", "hang"):
            hard_ax_cases.append({"surface": rh.get("exc") or (rh.get("stderr") or ""),
                                  "expected": ax.expected_action(diagnose._norm_fault(t_fault) if t_fault else None)})
        if rh.get("stderr"):
            ax_leaks.extend(ax.scan_leaks(rh["stderr"]))
    serviceability = {
        "original": diagnose.summarize_serviceability(scored_o),
        "hardened": diagnose.summarize_serviceability(scored_h),
    }
    ax_eval = {
        "observed": {
            "original": ax.evaluate_surfaces(orig_ax_cases) if orig_ax_cases else None,
            "hardened": ax.evaluate_surfaces(hard_ax_cases) if hard_ax_cases else None,
        },
        "catalog": ax.catalog_benchmark(),
        "leaks": sorted(set(ax_leaks)),
    }

    gates = judge.gates(happy, o, h, crashes, budget, outage)
    ai_eval = None
    if has_ai:
        clean_o = raw["results"][0::2]
        clean_tok = sum(int((a.get("ai_resp") or {}).get("input_tokens", 0)) + int((a.get("ai_resp") or {}).get("output_tokens", 0))
                        for r in clean_o for a in (r.get("attempts") or [])) // max(1, len(clean_o))
        clean_cost = sum(float((a.get("ai_resp") or {}).get("cost_usd", 0.0))
                         for r in clean_o for a in (r.get("attempts") or [])) / max(1, len(clean_o))
        ai_o = ai.summarize_ai_trials(sides["original"], clean_tokens=clean_tok, clean_cost=clean_cost)
        ai_h = ai.summarize_ai_trials(sides["hardened"], clean_tokens=clean_tok, clean_cost=clean_cost)
        inj_ok = (ai_h.get("injection_resistance") in (None, 1.0)) and not any(
            ("refund_all" in str(t.get("result") or "") or "issue_refund" in str(t.get("result") or "")
             or ai.DEFAULT_CANARY.lower() in str(t.get("result") or "").lower())
            and t.get("class") != "error"
            for t in sides["hardened"]
        )
        max_att = max((len(t.get("attempts") or []) for t in sides["hardened"]), default=0)
        gates["ai_budget"] = h["hang"] == 0 and max_att <= 20
        gates["ai_injection"] = inj_ok
        ai_eval = {"original": ai_o, "hardened": ai_h, "max_attempts_per_trial": max_att}
    return {
        "id": meta["id"], "lang": adapter.name, "budget_s": budget, "recorded": len(calls), "happy": happy,
        "chaos": {"calls": len(usable[:max_chaos]), "scenarios": scenarios, "by_scenario": rows, "first_failure": first_bad,
                  "first_original_dup": first_dup, "new_crashes": crashes},
        "original": o, "hardened": h, "outage": outage,
        "serviceability": serviceability, "ax": ax_eval, "ai": ai_eval,
        "gates": gates, "passed": all(gates.values()), "fallback": raw["fallback"],
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }


def _args(call: dict) -> str:
    return cassette.show({"$tuple": call["args"]}, 200) + (" " + cassette.show(call["kwargs"], 100) if call["kwargs"] else "")


def verify(fid: str | None = None, max_chaos: int = 60, quiet: bool = False) -> dict:
    project = find_project()
    fid = resolve_id(project, fid)
    d = harden_dir(project, fid)
    meta = load_json(d / "meta.json")
    adapter = lang.by_name(meta.get("lang"))
    hp = d / adapter.hardened_file(meta)
    if not hp.exists():
        raise UserError("%s is missing" % os.path.relpath(hp), "verify replays the calls through it", "harden %s" % fid)
    if not quiet:
        ui.headline("verify", "%s %s %d recorded calls %s %d faults + 3 seeded mixes %s virtual clock" % (
            fid, ui.DOT, meta["recorded"], ui.DOT, len(faults.MATRIX), ui.DOT))
    t0 = time.perf_counter()
    sha = sha256_file(hp)  # of the code that is replayed, not of whatever is there afterwards
    refused = adapter.refuse_hardened(hp)
    try:
        if refused:
            raise ReplayFailed({"stage": "accept %s" % hp.name, "message": refused})
        res = measure(adapter, project, d, meta, max_chaos)
        if sha256_file(hp) != sha:
            raise ReplayFailed({"stage": "verify %s" % hp.name, "message": "the file changed while it was being verified"})
    except ReplayFailed as e:
        res = {"id": fid, "passed": False, "fatal": e.fatal}
    res = scrub(res, project)
    res["hardened_sha256"] = sha
    res["verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    res["wall_s"] = round(time.perf_counter() - t0, 2)
    fm = {k: faults.DESCRIBE[k] for k in faults.MATRIX + ("mixed",)}
    if res.get("ai"):
        fm.update(ai.AI_FAULT_DESCRIBE)
    res["fault_model"] = fm
    save_json(d / "verify.json", res)
    if not res.get("fatal"):
        from . import export as _export
        _export.emit_event(project, "verify.passed" if res.get("passed") else "verify.failed",
                           {"availability": (res.get("hardened") or {}).get("availability")},
                           severity="INFO" if res.get("passed") else "WARN", function_id=fid)
        _export.sync_out(project)
    if not quiet:
        print_verify(res, fid, hp.name)
    return res


def _cls_line(s: dict) -> str:
    return " ".join("%s %d" % (k, s[k]) for k in CLASSES if s.get(k)) or "—"


def print_verify(res: dict, fid: str, hname: str) -> None:
    print()
    if res.get("fatal"):
        f = res["fatal"]
        print("  " + ui.bad(ui.FAIL + " couldn't %s" % f["stage"]))
        for ln in f["message"].strip().splitlines()[-8:]:
            print("    " + ui.dim(ln))
        ui.next_hint("verify %s   (after fixing %s)" % (fid, hname))
        return
    h, o, hp, g = res["hardened"], res["original"], res["happy"], res["gates"]
    budget = res["budget_s"]

    def mark(ok):
        return ui.good(ui.OK) if ok else ui.bad(ui.FAIL)

    def arrow(a, b, higher_better=True):
        if b == a:
            return " "
        return ui.good(ui.UP if b > a else ui.DOWN) if (b > a) == higher_better else ui.bad(ui.UP if b > a else ui.DOWN)

    nd = hp["original_nondeterministic"]
    rows = [
        ["happy-path replay (no faults)", "", "%d/%d identical" % (hp["equal"], hp["total"]), mark(g["happy"])],
        ["availability under faults", ui.pct(o["availability"]),
         arrow(o["availability"], h["availability"]) + " " + ui.bold(ui.pct(h["availability"])), mark(g["availability"])],
        ["silent wrong results", str(o["wrong"]), str(h["wrong"] + h["miss"]), mark(g["no_wrong"])],
        ["hangs", str(o["hang"]), str(h["hang"]), mark(g["no_hang"])],
        ["p99 latency (budget %s)" % ui.secs(budget), ui.secs(o["p99"]), ui.secs(h["p99"]), mark(g["p99"])],
        ["duplicate side effects", str(o["dups"]), str(h["dups"]), mark(g["dups"])],
    ]
    if o.get("amplification") is not None:
        rows.append(["attempts per call under faults", "%.2f" % o["amplification"], "%.2f" % h["amplification"], ui.dim("info")])
    if res.get("outage"):
        oo, oh = res["outage"]["original"], res["outage"]["hardened"]
        rows.append(["  with the dependency down", "%.2f" % oo["amplification"], "%.2f" % oh["amplification"], ui.dim("info")])
    if h.get("logged_failures") is not None or o.get("logged_failures") is not None:
        rows.append(["failures logged w/ trace id", ui.pct(o.get("logged_failures"), 0),
                     ui.pct(h.get("logged_failures"), 0), ui.dim("info")])
    srv = res.get("serviceability") or {}
    if srv.get("hardened") and srv["hardened"].get("runs"):
        so, sh = srv.get("original") or {}, srv["hardened"]
        rows.append(["root-cause diagnosis (logs)", ui.pct(so.get("diagnosis_accuracy"), 0),
                     ui.pct(sh.get("diagnosis_accuracy"), 0),
                     ui.dim("lines-to-diag %s → %s" % (so.get("lines_to_diagnosis_p50") or "—", sh.get("lines_to_diagnosis_p50") or "—"))])
    air = res.get("ai") or {}
    if air.get("hardened"):
        ao, ah = air.get("original") or {}, air["hardened"]
        rows.append(["AI semantic success", ui.pct(ao.get("semantic_success_rate"), 1),
                     ui.pct(ah.get("semantic_success_rate"), 1), ui.dim("info")])
        rows.append(["AI injection resistance", ui.pct(ao.get("injection_resistance"), 1),
                     ui.pct(ah.get("injection_resistance"), 1), mark(g.get("ai_injection", True))])
    axr = res.get("ax") or {}
    if axr.get("catalog"):
        co, ch = axr["catalog"]["original"], axr["catalog"]["hardened"]
        rows.append(["agent next-action accuracy", ui.pct(co["accuracy"], 0), ui.pct(ch["accuracy"], 0),
                     ui.dim("leaks: %d" % len(axr.get("leaks") or []))])
    print(ui.table(["gate", "original", "hardened", ""], rows, "lrrl"))
    print(ui.dim("  %d trials each: %d calls × %d fault scenarios · availability = (ok + degraded) / trials" % (
        h["trials"], res["chaos"]["calls"], len(res["chaos"]["scenarios"]))))
    print(ui.dim("  wrong = neither the real answer nor the declared fallback · hang = no timeout, or over budget"))
    if res.get("outage"):
        print(ui.dim("  dependency down: %d calls in a row, nothing reset in between · attempts per call = retry amplification" % res["outage"]["down"]["calls"]))
    if nd:
        print(ui.warn("  %d recorded calls excluded: the original itself didn't reproduce them" % nd))
    print()
    scn = res["chaos"]["by_scenario"]
    srows = []
    for name, r in scn.items():
        n = r["n"]
        ok_o = r["original"].get("ok", 0) + r["original"].get("degraded", 0)
        ok_h = r["hardened"].get("ok", 0) + r["hardened"].get("degraded", 0)
        srows.append([name, "%d/%d" % (ok_o, n), "%d/%d" % (ok_h, n), _cls_line({**{k: 0 for k in CLASSES}, **r["original"]}),
                      _cls_line({**{k: 0 for k in CLASSES}, **r["hardened"]})])
    print(ui.table(["fault", "orig avail", "hard avail", "original", "hardened"], srows, "lrrll"))
    fd = res["chaos"].get("first_original_dup")
    if fd:
        print()
        att = fd["original"]["attempts"]
        steps = ", ".join("%s #%d %s%s" % (a["method"] or "run", a["n"], a["fault"] or "ok",
                                          " (reached server)" if a["reached"] and a["fault"] else "") for a in att)
        hdr = "the original double-sends" if fd["original"]["dups"] else "hardened double-sends"
        print("  " + ui.warn(ui.WARN + " %s: call #%d %s under %s" % (hdr, fd["call"], ui.clip(fd["args"], 70), fd["scenario"])))
        print("    original: %s %s %d duplicate%s" % (steps, ui.ARROW, fd["original"]["dups"], "" if fd["original"]["dups"] == 1 else "s"))
        hatt = fd["hardened"]["attempts"]
        keys = {a["idem"] for a in hatt if a["reached"]}
        print("    hardened: %s %s %d duplicates%s" % (
            ", ".join("%s #%d %s" % (a["method"] or "run", a["n"], a["fault"] or "ok") for a in hatt), ui.ARROW,
            fd["hardened"]["dups"], (" (one Idempotency-Key: %s)" % ui.clip(str(next(iter(keys))), 32)) if len(keys) == 1 and None not in keys else ""))
    ff = res["happy"].get("first_failure") if not g["happy"] else None
    cf = res["chaos"].get("first_failure")
    if ff or (cf and not res["passed"]):
        print()
        if ff:
            print("  " + ui.bad("first failing call (happy path, no faults): #%d %s" % (ff["call"], ff["args"])))
            _explain(ff)
        else:
            print("  " + ui.bad("first failing call: #%d %s under %s" % (cf["call"], cf["args"], cf["scenario"])))
            print("    fault schedule: " + ", ".join("%s#%d=%s" % (_k(a["key"]), a["n"], a["fault"] or "ok")
                                                   for a in cf["hardened"]["attempts"]) or "    (no dependency calls)")
            _explain(cf["hardened"], cf["original"])
    print()
    if res["passed"]:
        print("  " + ui.good(ui.bold(ui.OK + " PASS")) + "  enable is unlocked for this %s " % hname +
              ui.dim("(sha256 %s…) %s saved verify.json" % (res["hardened_sha256"][:12], ui.DOT)))
        ui.next_hint("enable %s" % fid)
    else:
        failed = [k for k, v in g.items() if not v]
        print("  " + ui.bad(ui.bold(ui.FAIL + " FAIL")) + "  gates failed: " + ", ".join(failed) +
              ui.dim("  %s saved verify.json" % ui.DOT))
        ui.next_hint("verify %s   (after fixing .ras/hardens/%s/%s)" % (fid, fid, hname))


def _k(key: str) -> str:
    return key if len(key) <= 60 else key[:57] + "…"


def _explain(h: dict, o: dict | None = None) -> None:
    print("    hardened: %s %s" % (h["class"], ui.dim("(%s, %s)" % (h["exc"] or h["result"], ui.secs(h["latency"])))))
    if h.get("note"):
        print("    " + ui.warn(h["note"]))
    if h.get("miss"):
        print("    " + ui.dim(h["miss"]))
    print("    expected: %s" % ui.dim(str(h["expected"])))
    if o:
        print("    original: %s %s" % (o["class"], ui.dim("(%s, %s, %d dup)" % (o["exc"] or o["result"], ui.secs(o["latency"]), o["dups"]))))
    if h.get("dups"):
        print("    duplicate side effects: %d" % h["dups"])
