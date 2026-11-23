"""The pattern lab: every resilience pattern ON vs OFF under the same seeded trouble (`ras-anything bench --patterns`).

Two kinds of evidence, both from the real runtime code on a virtual clock:
  * scenarios: simulated workloads (outage, overload and retry storm, rate limit, ...) with many concurrent calls;
  * examples: the recorded calls of the example apps, replayed by verify with a hardening built on a Policy.
Every variant meets the same arrivals and the same faults (same seed), so differences are the pattern's. What counts
as better, worse and adopted is decided here, by the rule in `decide`, from the numbers alone (docs/PATTERNS.md).
"""

from __future__ import annotations

import json
import os
import platform
import re
import statistics
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import lang, patterns, ui
from .paths import UserError, load_json

# ------------------------------------------------------------------------------------------ scenarios

UP = {"service_s": 0.05}

SCENARIOS = {
    "healthy": {
        "what": "nothing fails, 20 calls/s: the happy path",
        "duration_s": 60, "patience_s": 5.0, "load": [{"dep": "api", "rps": 20}], "deps": {"api": UP}},
    "transient": {
        "what": "15% of attempts fail (refused, 503, slow +3 s, no answer), 20 calls/s",
        "duration_s": 60, "patience_s": 5.0, "load": [{"dep": "api", "rps": 20}],
        "deps": {"api": {**UP, "fault_rate": 0.15, "kinds": ["refused", "503", "slow", "hang"], "slow_s": 3.0}}},
    "slow tail": {
        "what": "4% of answers take +1.5 s, 20 calls/s",
        "duration_s": 60, "patience_s": 5.0, "load": [{"dep": "api", "rps": 20}],
        "deps": {"api": {**UP, "fault_rate": 0.04, "kinds": ["slow"], "slow_s": 1.5}}},
    "outage": {
        "what": "the dependency refuses everything for 30 s of 90 s, 30 calls/s, 40 different reads",
        "duration_s": 90, "patience_s": 5.0, "load": [{"dep": "api", "rps": 30, "keys": 40}], "window": [30, 60],
        "deps": {"api": {**UP, "phases": [{"from": 30, "to": 60, "mode": "refused"}]}}},
    "blackhole": {
        "what": "the dependency stops answering for 30 s of 90 s (no refusal, just silence), 30 calls/s",
        "duration_s": 90, "patience_s": 5.0, "load": [{"dep": "api", "rps": 30, "keys": 40}], "window": [30, 60],
        "deps": {"api": {**UP, "phases": [{"from": 30, "to": 60, "mode": "hang"}]}}},
    "retry storm": {
        "what": "overload: 85 calls/s into a server good for 100/s that runs 3x slower for 15 s of 120 s (queue of 400)",
        "duration_s": 120, "patience_s": 5.0, "load": [{"dep": "api", "rps": 85}], "window": [30, 45],
        "deps": {"api": {"service_s": 0.1, "workers": 10, "queue": 400, "phases": [{"from": 30, "to": 45, "slower": 3.0}]}}},
    "thundering herd": {
        "what": "400 calls at the same moment, server good for 100/s with a queue of 60",
        "duration_s": 30, "patience_s": 5.0, "load": [{"dep": "api", "from": 1.0, "burst": 400}],
        "deps": {"api": {"service_s": 0.1, "workers": 10, "queue": 60}}},
    "noisy neighbour": {
        "what": "one app, 24 slots, two dependencies: `slow` (10 calls/s) is silent for 40 s of 80 s, `fine` (25 calls/s) is healthy",
        "duration_s": 80, "patience_s": 5.0, "slots": 24, "window": [20, 60],
        "load": [{"dep": "slow", "rps": 10}, {"dep": "fine", "rps": 25}],
        "deps": {"slow": {**UP, "phases": [{"from": 20, "to": 60, "mode": "hang"}]}, "fine": UP}},
    "rate limit": {
        "what": "the server admits 25 calls per second and answers the rest with 429 + Retry-After: 1; 15 calls/s plus bursts of 60",
        "duration_s": 60, "patience_s": 5.0,
        "load": [{"dep": "api", "rps": 15}] + [{"dep": "api", "from": t, "burst": 60} for t in (10.0, 25.0, 40.0)],
        "deps": {"api": {**UP, "limit_rps": 25}}},
    "lost answers": {
        "what": "writes: 12% of attempts are done but the answer is lost, 5% refused, 10 writes/s; clients retry",
        "duration_s": 60, "patience_s": 5.0, "naive_writes": True, "load": [{"dep": "api", "op": "write", "rps": 10}],
        "deps": {"api": {**UP, "fault_rate": 0.17, "kinds": ["lost", "lost", "refused"], "hang_s": 60.0}}},
    "layered calls": {
        "what": "`front` calls `back`, both with retries; `back` is silent for 20 s of 60 s, 20 calls/s",
        "duration_s": 60, "patience_s": 5.0, "window": [20, 40], "load": [{"dep": "front", "rps": 20, "via": {"front": "back"}}],
        "deps": {"front": {"service_s": 0.02}, "back": {**UP, "phases": [{"from": 20, "to": 40, "mode": "hang"}]}},
        "params": {"back": {"attempt_timeout": {"timeout_s": 1.5}}}},
}

# ------------------------------------------------------------------------------------------ variants

OFF = {n: False for n in patterns.NAMES}
# The stack every pattern is measured in. Without `fallback`: a declared answer would hide what the others do.
STACK = {**OFF, "attempt_timeout": {"timeout_s": 2.0}, "deadline": True, "retry": True, "retry_budget": True, "retry_after": True,
         "circuit_breaker": True, "idempotency": True}
# The least a client does about failures: a timeout and three attempts.
BARE = {**OFF, "attempt_timeout": {"timeout_s": 2.0}, "retry": True}
ON = {"fallback": {"degraded": "degraded"}, "attempt_timeout": {"timeout_s": 2.0}, "hedge": {"delay_s": 0.5}}
LABEL = {"jitter": "Full jitter (vs none)"}
ROWS = (*patterns.NAMES, "jitter")


def variant(base: dict, name: str, on: bool) -> dict:
    """`base` with one pattern on or off."""
    if name == "jitter":
        return {**base, "retry": {**(base["retry"] if isinstance(base["retry"], dict) else {}), "full_jitter": on}}
    return {**base, name: ON.get(name, True) if on else False}


def variants() -> dict[str, dict]:
    """name -> patterns. "stack+x"/"stack-x", "bare+x"/"bare-x"; x on and off around both bases."""
    out = {}
    for bname, base in (("stack", STACK), ("bare", BARE)):
        out[bname] = base
        for n in ROWS:
            for on in (True, False):
                v = variant(base, n, on)
                if v != base and not (n == "jitter" and on):
                    out["%s%s%s" % (bname, "+" if on else "-", n)] = v
    return out


def pair(bname: str, name: str) -> tuple[str, str]:
    """The variant names to compare: (on, off)."""
    base = STACK if bname == "stack" else BARE
    if name == "jitter" or base[name]:
        return bname, "%s-%s" % (bname, name)
    return "%s+%s" % (bname, name), bname


# ------------------------------------------------------------------------------------------ metrics


def pctl(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def metrics(sc: dict, res: dict) -> dict:
    """One simulated run -> numbers. A call is answered when its answer came within the caller's patience."""
    calls = res["calls"]
    n = len(calls)
    c = {k: sum(x[2] == k for x in calls) for k in ("fresh", "stale", "degraded")}
    arrivals = [t for ts in res["arrivals"].values() for t in ts]
    layers = 1 + max((len(s.get("via", {})) for s in sc["load"]), default=0)
    out = {"calls": n, "availability": sum(c.values()) / n, "fresh": c["fresh"] / n, "p99_s": pctl([x[3] for x in calls], 0.99),
           "dups": res["dups"], "amplification": len(arrivals) / (n * layers), "wasted": res["wasted"] / n}
    if sc.get("window"):
        a, b = sc["window"]
        inside = [x for x in calls if a <= x[1] < b]
        out["outage_amplification"] = sum(a <= t < b for t in arrivals) / (len(inside) * layers)
        out["outage_availability"] = sum(x[2] in c for x in inside) / len(inside)
        out["recovery_s"] = recovery(calls, b, sc["duration_s"])
    return out


def recovery(calls, end, duration):
    """Seconds after the trouble ended until 95% of the calls of the next two seconds get a fresh answer."""
    for k in range(int(duration - end) - 1):
        part = [x for x in calls if end + k <= x[1] < end + k + 2]
        if part and sum(x[2] == "fresh" for x in part) >= 0.95 * len(part):
            return float(k)
    return float(duration - end)


# metric: (higher is better, a clear difference, relative?)
MARGINS = {"availability": (True, 0.005, False), "fresh": (True, 0.005, False), "p99_s": (False, 0.05, True), "dups": (False, 1.0, False),
           "amplification": (False, 0.05, True), "outage_amplification": (False, 0.05, True), "outage_availability": (True, 0.005, False),
           "wasted": (False, 0.02, False), "recovery_s": (False, 1.0, False)}
BETTER = ("availability", "p99_s", "dups", "amplification", "outage_amplification", "wasted", "recovery_s")
LOAD = ("p99_s", "amplification", "outage_amplification")  # what a pattern may spend, if it buys availability with it
AGREE = 0.8            # share of seeds that must agree on the direction
MAX_LOSS = 0.05        # of availability, in any one scenario
MAX_RECOVERY_S = 2.0   # longer until back to normal, in any one scenario
MAX_ON_BALANCE = 0.01  # of availability, clear changes of all scenarios added up


def effect(metric: str, on: list, off: list) -> int:
    """+1 better, -1 worse, 0 no (clear) difference, from paired seeds."""
    up, margin, rel = MARGINS[metric]
    a, b = statistics.mean(on), statistics.mean(off)
    if abs(a - b) < (margin * abs(b) if rel else margin) or a == b:
        return 0
    sign = 1 if a > b else -1
    if sum((x > y) if sign > 0 else (x < y) for x, y in zip(on, off)) < AGREE * len(on):
        return 0
    return sign if up else -sign


def compare(runs: dict, scenario: str, on: str, off: str) -> dict:
    a, b = runs[scenario][on], runs[scenario][off]
    out = {}
    for m in MARGINS:
        if m in a[0]:
            x, y = [r[m] for r in a], [r[m] for r in b]
            out[m] = {"on": statistics.mean(x), "off": statistics.mean(y), "effect": effect(m, x, y)}
    return out


def decide(cmp: dict, in_stack: bool) -> dict:
    """cmp: {base: {scenario: comparison}} -> {"verdict", "better", "costs", "because"}.

    rejected   nothing got clearly better anywhere.
    adopted    something did (in the stack, or against the bare client; a pattern that isn't part of the standard stack
               must add something to it), and in the stack: the healthy scenario is
               untouched, no duplicates were added, no scenario lost more than MAX_LOSS availability or took more than
               MAX_RECOVERY_S longer to recover, availability did not fall by more than MAX_ON_BALANCE on balance (the
               clear changes added up), and p99 or amplification only got worse if availability got better somewhere.
    opt-in     otherwise."""
    def hits(b, metrics, sign, where=lambda s: True):
        return [[b, s, m] for s in cmp[b] if where(s) for m in metrics if cmp[b][s].get(m, {}).get("effect") == sign]

    better = [x for b in cmp for x in hits(b, BETTER, 1)]
    st = cmp["stack"]
    avail = {s: st[s]["availability"]["on"] - st[s]["availability"]["off"] for s in st if st[s]["availability"]["effect"]}
    costs = [["stack", s, "availability"] for s, d in avail.items() if d < 0] + hits("stack", LOAD + ("dups", "recovery_s"), -1)
    because = []
    if not in_stack and not any(b == "stack" for b, _, _ in better):
        because.append("adds nothing clear to the stack")
    if hits("stack", BETTER, -1, lambda s: s == "healthy"):
        because.append("changes the healthy scenario")
    if hits("stack", ("dups",), -1):
        because.append("adds duplicate writes")
    worst = min(avail.items(), key=lambda kv: kv[1], default=("", 0.0))
    if worst[1] < -MAX_LOSS:
        because.append("costs %.1f points of availability in `%s`" % (-100 * worst[1], worst[0]))
    slow = [s for s in st if "recovery_s" in st[s] and st[s]["recovery_s"]["effect"] == -1
            and st[s]["recovery_s"]["on"] - st[s]["recovery_s"]["off"] > MAX_RECOVERY_S]
    if slow:
        because.append("slows recovery by more than %g s in `%s`" % (MAX_RECOVERY_S, slow[0]))
    if sum(avail.values()) < -MAX_ON_BALANCE:
        because.append("costs availability on balance (%+.1f points over the scenarios)" % (100 * sum(avail.values())))
    if hits("stack", LOAD, -1) and not any(d > 0 for d in avail.values()):
        because.append("raises p99 or load without buying availability")
    if not better:
        verdict, because = patterns.REJECTED, ["nothing got clearly better"]
    else:
        verdict = patterns.OPT_IN if because else patterns.ADOPTED
    return {"verdict": verdict, "better": better, "costs": costs, "because": because}


# ------------------------------------------------------------------------------------------ running


def simulate(adapter, specs: list[tuple[str, str, int]], vs: dict) -> list[dict]:
    """specs: (scenario, variant, seed) -> metrics, one each; spread over this machine's CPUs."""
    n = max(1, os.cpu_count() or 1)
    chunks = [specs[i::n] for i in range(n) if specs[i::n]]

    def work(chunk):
        with tempfile.TemporaryDirectory(prefix="ras-lab-") as d:
            job, out = Path(d) / "job.json", Path(d) / "out.json"
            job.write_text(json.dumps({"out": str(out), "runs": [
                {"scenario": lab_scenario(adapter, s), "patterns": adapter.lab_params(vs[v]), "seed": seed} for s, v, seed in chunk]}))
            argv, env = adapter.lab_cmd(job)
            p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=3600)
            if p.returncode:
                raise UserError("the %s lab worker failed" % adapter.label, p.stderr.strip()[-800:], "doctor")
            return [metrics(SCENARIOS[s], r) for (s, _, _), r in zip(chunk, json.loads(out.read_text()))]

    done: dict = {}
    with ThreadPoolExecutor(n) as ex:
        for chunk, ms in zip(chunks, ex.map(work, chunks)):
            done.update(zip(chunk, ms))
    return [done[s] for s in specs]


def lab_scenario(adapter, name: str) -> dict:
    sc = SCENARIOS[name]
    return {**sc, "params": {d: adapter.lab_params(p) for d, p in sc.get("params", {}).items()}}




def judge_patterns(runs: dict) -> dict:
    """{row: {"verdict", "better", "costs", "because", "compare"}} for one language's runs."""
    out = {}
    for n in ROWS:
        cmp = {}
        for b in ("stack", "bare"):
            on, off = pair(b, n)
            cmp[b] = {s: compare(runs, s, on, off) for s in SCENARIOS}
        out[n] = {**decide(cmp, n == "jitter" or bool(STACK[n]) or n == "fallback"), "compare": cmp}
    return out


# ------------------------------------------------------------------------------------------ example apps

EXAMPLES = (("weather", "weather.fetch_forecast"), ("payments", "billing.charge"), ("orders-node", "orders.getItem"), ("orders-node", "orders.charge"))
EXAMPLE_STACK = {**{k: bool(v) for k, v in STACK.items()}, "fallback": True}
EXAMPLE_METRICS = ("availability", "p99", "dups", "amplification", "outage_amplification", "outage_answered", "recovered_after_s", "wrong", "hang")


def example_variants() -> dict[str, tuple[str, dict]]:
    """name -> (pattern it is about, patterns): the stack with one pattern switched."""
    out = {"stack": ("", EXAMPLE_STACK)}
    for n in ROWS:
        if n == "jitter":
            out["stack-jitter"] = (n, {**EXAMPLE_STACK, "retry": {"full_jitter": False}})
        else:
            on = EXAMPLE_STACK[n]
            out["stack%s%s" % ("-" if on else "+", n)] = (n, {**EXAMPLE_STACK, n: not on})
    return out


def examples(root: Path) -> dict:
    """Every example function under every variant: {fid: {variant: numbers}}, measured by verify on its recorded calls."""
    from .verify import measure

    vs = example_variants()

    def one(job):
        (example, fid), name = job
        project = root / example
        d = project / ".ras" / "hardens" / fid
        meta = load_json(d / "meta.json")
        adapter = lang.by_name(meta["lang"])
        hardened = adapter.hardened_file(meta)
        text = (root / "patterns-lab" / "hardened" / (fid + Path(hardened).suffix)).read_text()
        lines = [adapter.lab_line(ln, vs[name][1]) if "ras-lab" in ln else ln for ln in text.splitlines()]
        with tempfile.TemporaryDirectory(prefix="ras-lab-") as tmp:
            (Path(tmp) / "calls.json").write_bytes((d / "calls.json").read_bytes())
            (Path(tmp) / hardened).write_text("\n".join(lines) + "\n")
            v = measure(adapter, project, Path(tmp), meta, 60)
        h, o = v["hardened"], v["outage"]["hardened"]
        return {"happy": "%d/%d" % (v["happy"]["equal"], v["happy"]["total"]), "passed": v["passed"],
                "failed_gates": [k for k, ok in v["gates"].items() if not ok], "trials": h["trials"],
                "availability": h["availability"], "p99": h["p99"], "dups": h["dups"] + o["dups"], "amplification": h["amplification"],
                "outage_amplification": o["amplification"], "outage_answered": (o["ok"] + o["degraded"]) / o["calls"],
                "recovered_after_s": o["recovered_after_s"], "wrong": h["wrong"] + o["wrong"], "hang": h["hang"] + o["hang"]}

    jobs = [(e, v) for e in EXAMPLES for v in vs]
    out: dict = {}
    with ThreadPoolExecutor(max(1, (os.cpu_count() or 2) // 2)) as ex:
        for ((_, fid), name), r in zip(jobs, ex.map(one, jobs)):
            out.setdefault(fid, {})[name] = r
    return out


# ------------------------------------------------------------------------------------------ results and tables

SEED0 = 1000  # seeds are SEED0+1 ...: none of them was used while the defaults were being tuned
RANK = {patterns.ADOPTED: 0, patterns.OPT_IN: 1, patterns.REJECTED: 2}
# The scenario each pattern is meant for: the one the summary shows.
MEANT_FOR = {"fallback": "outage", "deadline": "layered calls", "bulkhead": "noisy neighbour", "idempotency": "lost answers",
             "retry_budget": "outage", "retry_after": "rate limit", "retry": "transient", "circuit_breaker": "retry storm",
             "adaptive_throttle": "retry storm", "adaptive_concurrency": "retry storm", "hedge": "slow tail", "attempt_timeout": "blackhole",
             "jitter": "outage"}


def run(seeds: int = 20, out: Path | None = None, examples_root: Path | None = None, quiet: bool = False) -> dict:
    """`ras-anything bench --patterns`."""
    global_seeds = range(SEED0 + 1, SEED0 + seeds + 1)
    vs = variants()
    if not quiet:
        ui.headline("bench --patterns", "%d scenarios %s %d variants %s %d seeds %s virtual clock" % (
            len(SCENARIOS), ui.DOT, len(vs), ui.DOT, seeds, ui.DOT))
    t0 = time.perf_counter()
    langs = {}
    for adapter in lang.ADAPTERS:
        if adapter.lab_cmd(Path("job.json")) is None:
            continue
        specs = [(s, v, seed) for s in SCENARIOS for v in vs for seed in global_seeds]
        runs: dict = {}
        for (s, v, _), m in zip(specs, simulate(adapter, specs, vs)):
            runs.setdefault(s, {}).setdefault(v, []).append(m)
        langs[adapter.name] = {"patterns": judge_patterns(runs), "base": {s: {b: {m: statistics.mean(r[m] for r in runs[s][b]) for m in runs[s][b][0]}
                                                                              for b in ("bare", "stack")} for s in SCENARIOS}}
        if not quiet:
            print("  %s %s runtime measured %s" % (ui.good(ui.OK), adapter.label, ui.dim("%.0fs" % (time.perf_counter() - t0))), flush=True)
    verdicts = {n: max((x["patterns"][n]["verdict"] for x in langs.values()), key=RANK.get) for n in ROWS}
    root = examples_root or Path(__file__).resolve().parent.parent / "examples"
    ex = examples(root) if (root / "patterns-lab" / "hardened").is_dir() else None
    res = {"seeds": [SEED0 + 1, SEED0 + seeds], "verdicts": verdicts, "languages": _round(langs), "examples": _round(ex),
           "scenarios": {s: x["what"] for s, x in SCENARIOS.items()}, "stack": STACK, "bare": BARE,
           "margins": {m: {"higher_is_better": a, "clear": b, "relative": r} for m, (a, b, r) in MARGINS.items()},
           "host": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
           "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "wall_s": round(time.perf_counter() - t0)}
    out = out or Path(".ras") / "patterns-lab"
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(res, separators=(",", ":")).replace('"languages"', '\n"languages"').replace('"examples"', '\n"examples"') + "\n")
    (out / "RESULTS.md").write_text(results_md(res))
    if not quiet:
        print()
        print(ui.table(["pattern", "verdict", "meant for", "availability off → on", "p99", "attempts/call"], [
            [n, v, MEANT_FOR[n], *_terminal(res, n)] for n, v in verdicts.items()], "llllll"))
        print()
        print(ui.dim("  same arrivals and faults for every variant · saved %s" % os.path.relpath(out / "results.json")))
    return res


def _round(x):
    if isinstance(x, float):
        return round(x, 4)
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_round(v) for v in x]
    return x


def _terminal(res, n):
    c = next(iter(res["languages"].values()))["patterns"][n]["compare"][_shown(res, n)][MEANT_FOR[n]]
    amp = c.get("outage_amplification") or c["amplification"]
    return ["%s → %s" % (ui.pct(c["availability"]["off"]), ui.pct(c["availability"]["on"])),
            "%s → %s" % (ui.secs(c["p99_s"]["off"]), ui.secs(c["p99_s"]["on"])), "%.2f → %.2f" % (amp["off"], amp["on"])]


def _shown(res, n) -> str:
    """The base to show a pattern against: the stack, unless another pattern of the stack hides all it does."""
    for b in ("stack", "bare"):
        if any(x["patterns"][n]["compare"][b][MEANT_FOR[n]][m]["effect"] for x in res["languages"].values()
               for m in x["patterns"][n]["compare"][b][MEANT_FOR[n]] if m in BETTER):
            return b
    return "stack"


FMT = {"availability": "%.1f%%", "fresh": "%.1f%%", "outage_availability": "%.1f%%", "wasted": "%.1f%%", "p99_s": "%.2f s", "recovery_s": "%.1f s", "dups": "%.1f"}


def _v(m, x) -> str:
    return (FMT.get(m, "%.2f")) % (100 * x if FMT.get(m, "").endswith("%%") else x)


def _cell(c, m) -> str:
    """"off → on", bold when clearly better, marked when clearly worse."""
    if m not in c:
        return "—"
    off, on, e = _v(m, c[m]["off"]), _v(m, c[m]["on"]), c[m]["effect"]
    return "%s → %s" % (off, {1: "**%s**", -1: "%s ▼", 0: "%s"}[e] % on)


def title(n: str) -> str:
    return LABEL.get(n) or patterns.get(n).title


def _adj(n) -> str:
    return {"stack": "in the stack", "bare": "bare client"}[n]


def summary_table(res: dict) -> str:
    """One row per pattern: the scenario it is meant for, off → on, per language; the verdict and why."""
    names = list(res["languages"])
    out = ["| pattern | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | verdict |",
           "|---|---|---|---|---|---|---|---|---|"]
    for n in ROWS:
        b, s = _shown(res, n), MEANT_FOR[n]
        for i, lg in enumerate(names):
            c = res["languages"][lg]["patterns"][n]["compare"][b][s]
            out.append("| %s | %s | %s | %s | %s |" % (
                "**%s**<br>`%s`" % (title(n), n) if i == 0 else "", "%s, %s" % (s, _adj(b)) if i == 0 else "", lg,
                " | ".join(_cell(c, m) for m in ("availability", "p99_s", "dups", "amplification", "outage_amplification")),
                "**%s**" % res["verdicts"][n] if i == 0 else ""))
    return "\n".join(out)


def reasons_table(res: dict) -> str:
    """The verdicts' arithmetic: what got better, what it costs, what decided."""
    out = ["| pattern | verdict | clearly better (best three) | costs, in the stack (worst three) | not adopted because |", "|---|---|---|---|---|"]
    for n in ROWS:
        better, costs, because = [], [], []
        for lg, x in res["languages"].items():
            p = x["patterns"][n]

            def size(hit, p=p):
                c = p["compare"][hit[0]][hit[1]][hit[2]]
                rel = MARGINS[hit[2]][2]
                return abs(c["on"] - c["off"]) / ((abs(c["off"]) or 1e-9) if rel else MARGINS[hit[2]][1] * 20)

            def say(hits, lg=lg, p=p):
                return ["%s `%s` %s %s" % ("" if b == "stack" else "(bare)", s, m, _cell({k: {**v, "effect": 0} for k, v in p["compare"][b][s].items()}, m))
                        for b, s, m in sorted(hits, key=size, reverse=True)[:3]]
            if lg == next(iter(res["languages"])):
                better, costs = say(p["better"]), say(p["costs"])
            because += [w for w in p["because"] if w not in because]
        out.append("| %s | **%s** | %s | %s | %s |" % (title(n), res["verdicts"][n], "<br>".join(better) or "nothing", "<br>".join(costs) or "none", "; ".join(because) or "—"))
    return "\n".join(out)


def examples_table(res: dict) -> str:
    if not res.get("examples"):
        return "(not measured)"
    out = ["| function | variant | happy path | availability | p99 | dups | wrong | hangs | attempts per call | … in an outage | answered in the outage | back after | verify |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for fid, vs in res["examples"].items():
        base = vs["stack"]
        for name, r in vs.items():
            def mark(text, k, fid=fid):
                return "**%s**" % text if name != "stack" and r[k] != base[k] else text
            out.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                "`%s`" % fid if name == "stack" else "", "stack" if name == "stack" else name[5] + " " + title(name[6:]), r["happy"],
                mark("%.1f%%" % (100 * r["availability"]), "availability"), mark("%.2f s" % r["p99"], "p99"), mark(str(r["dups"]), "dups"),
                mark(str(r["wrong"]), "wrong"), mark(str(r["hang"]), "hang"), mark("%.2f" % r["amplification"], "amplification"),
                mark("%.2f" % r["outage_amplification"], "outage_amplification"), mark("%.0f%%" % (100 * r["outage_answered"]), "outage_answered"),
                mark("never" if r["recovered_after_s"] is None else "%.1f s" % r["recovered_after_s"], "recovered_after_s"),
                "✓" if r["passed"] else "✗ " + ", ".join(r["failed_gates"])))
    return "\n".join(out)


def base_table(res: dict) -> str:
    out = ["| scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |", "|---|---|---|---|---|---|---|---|---|"]
    for s in res["scenarios"]:
        for lg, x in res["languages"].items():
            a, b = x["base"][s]["bare"], x["base"][s]["stack"]
            out.append("| %s | %s | %s |" % (s, lg, " | ".join(
                "—" if m not in a else "%s → %s" % (_v(m, a[m]), _v(m, b[m])) for m in
                ("availability", "p99_s", "dups", "amplification", "outage_amplification", "wasted", "recovery_s"))))
    return "\n".join(out)


def detail_tables(res: dict) -> str:
    out = []
    for n in ROWS:
        out += ["### %s (`%s`): %s\n" % (title(n), n, res["verdicts"][n]),
                "| base | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |", "|---|---|---|---|---|---|---|---|---|---|"]
        for b in ("stack", "bare"):
            for s in res["scenarios"]:
                for lg, x in res["languages"].items():
                    c = x["patterns"][n]["compare"][b][s]
                    if any(c[m]["effect"] for m in c if m in BETTER):
                        out.append("| %s | %s | %s | %s |" % (b, s, lg, " | ".join(_cell(c, m) for m in (
                            "availability", "p99_s", "dups", "amplification", "outage_amplification", "wasted", "recovery_s"))))
        out.append("")
    return "\n".join(out)


SECTIONS = {"summary": summary_table, "reasons": reasons_table, "examples": examples_table, "base": base_table}


def results_md(res: dict) -> str:
    return "\n".join([
        "# Pattern lab results\n",
        "Made by `ras-anything bench --patterns --seeds %d` on %s %s, Python %s, %s. Seeds %d to %d, virtual clock, so the numbers are "
        "reproducible. `off → on`, means over the seeds; **bold** = clearly better, ▼ = clearly worse (rule: docs/PATTERNS.md).\n" % (
            res["seeds"][1] - res["seeds"][0] + 1, res["host"]["system"], res["host"]["machine"], res["host"]["python"], res["created"][:10], *res["seeds"]),
        "## Each pattern in the scenario it is meant for\n", summary_table(res), "", "## Verdicts\n", reasons_table(res), "",
        "## The bare client → the stack\n", base_table(res), "", "## The example apps\n", examples_table(res), "",
        "## Every clear difference\n", detail_tables(res)])


def fill(text: str, res: dict) -> str:
    """A document with its `<!-- lab:name -->` … `<!-- /lab -->` regions filled in from the results."""
    # The close marker may sit on the next line of an empty region (`<!-- lab:x -->\n<!-- /lab -->`);
    # requiring a newline *before* `<!-- /lab -->` after the open tag's newline would skip the pair
    # and let .*? swallow the next section up to a later close marker.
    return re.sub(
        r"(<!-- lab:(\w+) -->\n)(.*?)(<!-- /lab -->)",
        lambda m: m[1] + SECTIONS[m[2]](res) + "\n" + m[4],
        text,
        flags=re.S,
    )
