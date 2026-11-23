"""V-16.4 Flight-recorder replay & Root-Cause Analysis (RCA): `ras-anything replay <incident-id>`."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import cassette, diagnose, lang, ui, verify
from .paths import UserError, find_project, harden_dir, load_json, save_json, scrub

_FAULT_MAP = {
    "timeout": "timeout",
    "hang": "timeout",
    "refused": "refused",
    "circuit_open": "refused",
    "rejected": "refused",
    "unavailable": "http5xx",
    "rate_limited": "http429",
    "truncated": "truncated",
    "slow": "slow",
}

_FIXES = {
    "timeout": "Check dependency latency or raise attempt_timeout / deadline; keep a declared fallback for hard timeouts.",
    "refused": "Verify upstream availability/endpoint; ensure circuit_breaker + declared fallback handle connection refused.",
    "unavailable": "Upstream returned 5xx/reset; verify retry with jitter + circuit_breaker + degraded fallback.",
    "rate_limited": "Upstream throttled requests (429); honour Retry-After and enforce retry_budget / adaptive_throttle.",
    "truncated": "Upstream closed early or returned malformed body; include SyntaxError/JSONDecodeError in retry_on.",
    "circuit_open": "Circuit breaker was open to shed load; wait for cooldown or verify upstream recovery.",
    "approval": "Irreversible operation requires human authorization via require_approval / .ras/approvals.json.",
    "invalid_request": "Request rejected (4xx); fix request parameters/schema rather than retrying.",
    "auth": "Authentication/authorization failed (401/403); escalate credentials or permissions.",
    "quota": "Quota or budget exhausted; escalate quota or switch fallback.",
    "bug": "Unhandled programming error in hardened code; fix the bug and re-run verify before enabling.",
}


def incident_dir(project: Path) -> Path:
    env = os.environ.get("RAS_ANYTHING_INCIDENTS")
    if env and env.lower() not in ("off", "0", "no"):
        return Path(env)
    return project / ".ras" / "incidents"


def list_incidents(project: Path) -> list[dict]:
    d = incident_dir(project)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("inc-*.json"), reverse=True):
        doc = load_json(p)
        if isinstance(doc, dict) and doc.get("kind") == "incident":
            out.append({
                "id": doc.get("id") or p.stem,
                "path": str(p),
                "function": doc.get("function"),
                "lang": doc.get("lang"),
                "dependency": doc.get("dependency"),
                "fault": doc.get("fault"),
                "trigger": doc.get("trigger"),
                "created": doc.get("created"),
            })
    return out


def load_incident(incident_id: str | None, project: Path) -> tuple[Path, dict]:
    if incident_id:
        p = Path(incident_id)
        if p.is_file():
            doc = load_json(p)
            if isinstance(doc, dict):
                return p, doc
        d = incident_dir(project)
        for cand in (d / incident_id, d / (incident_id + ".json")):
            if cand.is_file():
                doc = load_json(cand)
                if isinstance(doc, dict):
                    return cand, doc
        raise UserError("incident %s not found" % incident_id,
                        "incidents live in %s" % d, "replay --list")
    items = list_incidents(project)
    if not items:
        raise UserError("no recorded incidents", "incidents are written to .ras/incidents/ when a hardened call fails",
                        "status")
    p = Path(items[0]["path"])
    return p, load_json(p)


def _canon_simple(v):
    if isinstance(v, dict):
        if len(v) == 1 and next(iter(v)) == "$tuple":
            return [_canon_simple(x) for x in v["$tuple"]]
        return {str(k): _canon_simple(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, list):
        return [_canon_simple(x) for x in v]
    return v


def _call_arg_hash(call: dict) -> str:
    try:
        args = call.get("args") or []
        kwargs = call.get("kwargs") or {}
        vals = list(args) + [v for _, v in sorted(kwargs.items())] if kwargs else list(args)
        text = json.dumps(_canon_simple(vals), sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(text.encode()).hexdigest()[:16]
    except Exception:
        return "?"


def _match_call(inc: dict, calls: list[dict]) -> int:
    want = inc.get("input_hash")
    if want:
        for i, c in enumerate(calls):
            if _call_arg_hash(c) == want:
                return i
    # Fallback: match serialized input representation substring
    inputs = inc.get("inputs")
    if isinstance(inputs, dict) and inputs:
        for i, c in enumerate(calls):
            c_txt = json.dumps(c.get("args") or [])
            for v in inputs.values():
                if isinstance(v, (str, int)) and str(v) in c_txt and v != "[redacted]":
                    return i
    return 0


def _plan_from_incident(inc: dict, call: dict, project: Path) -> dict:
    events = inc.get("events") or []
    seq = []
    for ev in events:
        if ev.get("event") == "failure" and ev.get("fault"):
            mapped = _FAULT_MAP.get(ev["fault"])
            if mapped:
                seq.append(mapped)
    if not seq and inc.get("fault"):
        mapped = _FAULT_MAP.get(inc["fault"])
        if mapped:
            seq.append(mapped)
    if not seq:
        return verify.NONE
    dep = inc.get("dependency")
    target_key = None
    if dep:
        for item in call.get("io") or []:
            k = cassette.text((item.get("req") or {}).get("key", ""), project)
            if diagnose._dep_matches(dep, k):
                target_key = k
                break
    return {"type": "schedule", "faults": seq, "key": target_key, "repeat": True}


def _decision_chain(inc: dict) -> list[dict]:
    chain = []
    for g in inc.get("guards_at_start") or []:
        chain.append({"t": 0.0, "step": "guard_at_start", "detail": "%s %s=%s" % (g.get("kind"), g.get("name"), g.get("state"))})
    for ev in inc.get("events") or []:
        kind = ev.get("event")
        t = ev.get("t", 0.0)
        if kind == "failure":
            detail = "attempt #%s to %s failed (%s)" % (ev.get("n", "?"), ev.get("dependency") or "dep", ev.get("fault") or "?")
        elif kind == "retry":
            detail = "scheduled retry #%s after %ss" % (ev.get("n", "?"), ev.get("delay_s", 0))
        elif kind == "gave_up":
            detail = "gave up after %s attempt(s): %s (%s)" % (ev.get("n", "?"), ev.get("fault") or "?", ev.get("why") or "?")
        elif kind == "breaker":
            detail = "breaker %s: %s -> %s" % (ev.get("name") or "?", ev.get("before"), ev.get("after"))
        elif kind == "slow":
            detail = "slow answer on attempt #%s (%ss)" % (ev.get("n", "?"), ev.get("took_s", "?"))
        elif kind == "recovered":
            detail = "recovered on attempt #%s" % ev.get("n", "?")
        elif kind == "log":
            detail = "logged event %s" % ev.get("name")
        else:
            detail = json.dumps({k: v for k, v in ev.items() if k not in ("t", "event")})
        chain.append({"t": t, "step": kind, "detail": detail})
    if not chain:
        out = inc.get("outcome") or {}
        chain.append({"t": inc.get("duration_s", 0.0), "step": inc.get("trigger") or "outcome",
                      "detail": "%s: %s" % (out.get("error_type") or out.get("kind"), out.get("message") or "")})
    return chain


def _first_divergent(inc: dict) -> str:
    guards = inc.get("guards_at_start") or []
    open_g = [g for g in guards if g.get("state") == "open"]
    if open_g:
        return "guard %s (%s) was already open at call start" % (open_g[0].get("name"), open_g[0].get("kind"))
    events = inc.get("events") or []
    fails = [e for e in events if e.get("event") == "failure"]
    gave = next((e for e in events if e.get("event") == "gave_up"), None)
    if fails:
        first = fails[0]
        dep = first.get("dependency") or inc.get("dependency") or "dependency"
        flt = first.get("fault") or inc.get("fault") or "error"
        if gave:
            return "attempt #%s to %s failed with %s; %s" % (first.get("n", 1), dep, flt, gave.get("why") or "exhausted")
        return "attempt #%s to %s failed with %s" % (first.get("n", 1), dep, flt)
    fault = inc.get("fault") or "unknown"
    out = inc.get("outcome") or {}
    if fault == "bug":
        return "unhandled %s in hardened code: %s" % (out.get("error_type") or "bug", (out.get("message") or "")[:120])
    if fault == "approval":
        return "irreversible operation blocked by human gate (%s)" % (out.get("error_type") or "ApprovalRequired")
    return "trigger=%s fault=%s (%s)" % (inc.get("trigger"), fault, out.get("error_type") or out.get("kind"))


def _did_reproduce(inc: dict, trial: dict | None, diag: dict | None) -> bool:
    if not trial:
        return False
    inc_fault = inc.get("fault")
    inc_err = (inc.get("outcome") or {}).get("error_type")
    if trial.get("outcome") == "hang" and inc_fault in ("timeout", "hang"):
        return True
    if inc_fault == "bug":
        return trial.get("outcome") == "raise" and (not inc_err or trial.get("exc_type") == inc_err)
    if trial.get("outcome") == "raise":
        if diag and diag.get("detected") and diagnose._fault_matches(diag.get("fault_class"), inc_fault):
            return True
        if inc_err and trial.get("exc_type") == inc_err:
            return True
        return True
    if trial.get("is_fallback") or (diag and diag.get("detected")):
        if inc.get("trigger") == "gave_up":
            return True
        if diag and diagnose._fault_matches(diag.get("fault_class"), inc_fault):
            return True
    return False


def replay(incident_id: str | None = None, project: Path | None = None, quiet: bool = False) -> dict:
    """Deterministically replay a recorded incident on the virtual clock and write an RCA report."""
    t0 = time.perf_counter()
    project = project or find_project()
    inc_path, inc = load_incident(incident_id, project)
    iid = inc.get("id") or inc_path.stem
    fid = inc.get("function")
    if not fid:
        raise UserError("incident %s has no function id" % iid, "cannot replay without target function", "replay --list")

    d = harden_dir(project, fid)
    meta = load_json(d / "meta.json")
    calls = cassette.load(d / "calls.json")
    adapter = lang.by_name(meta.get("lang") or inc.get("lang"))
    call_idx = _match_call(inc, calls)
    plan = _plan_from_incident(inc, calls[call_idx], project)
    deps = meta.get("dependencies") or cassette.dependencies(calls, project)
    meta_run = {**meta, "dependencies": deps}
    seed = int(inc.get("seed") or 0) & 0xFFFFFFFF

    trial = None
    diag = None
    replay_err = None
    try:
        raw = verify.replay(adapter, project, d, meta_run,
                            [{"call": call_idx, "side": "hardened", "plan": plan, "seed": seed}],
                            time.perf_counter() + 120.0)
        trial = (raw.get("results") or [None])[0]
        if trial:
            diag = diagnose.diagnose(trial.get("stderr") or "")
    except verify.ReplayFailed as e:
        replay_err = e.fatal.get("message")

    reproduced = _did_reproduce(inc, trial, diag)
    fault = inc.get("fault") or (diag or {}).get("fault_class") or "unknown"
    dep = inc.get("dependency") or (diag or {}).get("dependency")
    chain = _decision_chain(inc)
    first_div = _first_divergent(inc)
    fix = _FIXES.get(fault, "Inspect the decision chain and verify handling for this fault class.")
    diagnose_s = round(time.perf_counter() - t0, 4)

    rca = {
        "schema_version": "1.0.0",
        "kind": "replay",
        "incident_id": iid,
        "function": fid,
        "lang": adapter.name,
        "trace_id": inc.get("trace_id"),
        "seed": inc.get("seed"),
        "call_index": call_idx,
        "plan": plan,
        "failing_dependency": dep,
        "fault_type": fault,
        "reproduced": reproduced,
        "replay_outcome": trial.get("outcome") if trial else "error",
        "replay_error": replay_err or (trial.get("exc") if trial else None),
        "replay_diagnosis": diag,
        "decision_chain": chain,
        "first_divergent_decision": first_div,
        "suggested_fix": fix,
        "diagnose_s": diagnose_s,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    rca = scrub(rca, project)

    # Save machine-readable & markdown RCA
    out_dir = project / ".ras" / "out"
    if (project / ".ras").is_dir():
        save_json(out_dir / ("replay-%s.json" % iid), rca)
        from . import export as _export
        _export.emit_event(project, "replay.completed", {"incident": iid, "reproduced": bool(rca.get("reproduced"))},
                           trace_id=rca.get("trace_id"), function_id=fid)
        _export.sync_out(project)
        inc_md = inc_path.with_suffix(".rca.md")
        inc_md.write_text(_rca_markdown(rca), encoding="utf-8")

    if not quiet:
        print_rca(rca)
    return rca


def _rca_markdown(rca: dict) -> str:
    lines = [
        "# Incident RCA: `%s`" % rca["incident_id"],
        "",
        "- **Function:** `%s` (%s)" % (rca["function"], rca["lang"]),
        "- **Trace ID:** `%s`" % (rca.get("trace_id") or "—"),
        "- **Failing dependency:** `%s`" % (rca.get("failing_dependency") or "—"),
        "- **Fault type:** `%s`" % rca["fault_type"],
        "- **Reproduced (virtual clock):** %s (`%s`)" % ("yes" if rca["reproduced"] else "no", rca["replay_outcome"]),
        "- **First divergent decision:** %s" % rca["first_divergent_decision"],
        "- **Suggested fix:** %s" % rca["suggested_fix"],
        "- **Time to diagnose:** %.3fs" % rca["diagnose_s"],
        "",
        "## Decision chain",
        "",
    ]
    for c in rca["decision_chain"]:
        lines.append("- `+%ss` **%s**: %s" % (c["t"], c["step"], c["detail"]))
    lines.append("")
    return "\n".join(lines)


def print_rca(rca: dict) -> None:
    ui.headline("replay", rca["incident_id"])
    rep = ui.good(ui.OK + " reproduced") if rca["reproduced"] else ui.warn(ui.WARN + " not reproduced")
    rows = [
        ["function", "%s (%s)" % (rca["function"], rca["lang"])],
        ["dependency", str(rca.get("failing_dependency") or "—")],
        ["fault type", str(rca["fault_type"])],
        ["reproduction", "%s (%s, seed %s, call #%d)" % (rep, rca["replay_outcome"], rca.get("seed"), rca["call_index"])],
        ["first divergent", rca["first_divergent_decision"]],
        ["suggested fix", rca["suggested_fix"]],
        ["trace id", str(rca.get("trace_id") or "—")],
        ["time to diagnose", ui.secs(rca["diagnose_s"])],
    ]
    print(ui.table(["field", "value"], rows, "ll"))
    print()
    print("  decision chain:")
    for c in rca["decision_chain"]:
        print("    +%-6s %-16s %s" % ("%ss" % c["t"], c["step"], c["detail"]))


def print_list(project: Path | None = None) -> list[dict]:
    project = project or find_project()
    items = list_incidents(project)
    ui.headline("incidents", str(incident_dir(project)))
    if not items:
        print("  no recorded incidents")
        return items
    rows = [[it["id"], it["function"] or "?", it["lang"] or "?", it["dependency"] or "—", it["fault"] or "?", it["trigger"] or "?"] for it in items]
    print(ui.table(["id", "function", "lang", "dependency", "fault", "trigger"], rows, "llllll"))
    ui.next_hint("replay %s" % items[0]["id"])
    return items
