"""Turns raw facts from the agents into verdicts: what each trial was, the gates, the availability.

Everything here is arithmetic on JSON and the same for every language. An agent reports what a call
did (returned, raised, hung; equal to the recorded result or not; every dependency attempt it made);
deciding what that is worth happens here, outside the process that runs the code being judged.
"""

from __future__ import annotations

from . import cassette

CLASSES = ("ok", "degraded", "wrong", "error", "hang", "miss")


def duplicates(attempts: list) -> int:
    """Non-idempotent requests that reached the server more than once as different operations."""
    groups: dict = {}
    for n, a in enumerate(attempts or []):
        if a.get("nonidempotent") and a.get("reached"):
            groups.setdefault(a["key"], set()).add(a.get("idem") or "<no key #%d>" % n)
    return sum(len(g) - 1 for g in groups.values())


def keys_of(call: dict, project) -> list[str]:
    return list(dict.fromkeys(cassette.text(x["req"]["key"], project) for x in call["io"]))


def unearned(r: dict, keys: list[str]) -> str | None:
    """Why a returned fallback doesn't count in this trial, or None if it does. The evidence is what the
    agent saw at the boundary; what the code logged is only an extra requirement."""
    if not any(a["failed"] for a in r["attempts"]):
        return "returned RAS_FALLBACK although no dependency request failed in this call"
    answered = {a["key"] for a in r["attempts"] if not a["failed"]}
    if all(k in answered for k in keys):
        return "returned RAS_FALLBACK although every request was answered in the end"
    if not r["logs"]:
        return "returned RAS_FALLBACK without logging it (log_event)"
    return None


def classify_trial(r: dict, call: dict, keys: list[str], budget: float, fallback_allowed: bool) -> dict:
    """Adds class/note/dups to a raw trial result.

    ok        the recorded result (or the recorded kind of exception)
    degraded  the declared fallback, earned (see unearned)
    wrong     any other value: silent bad data
    error     an exception within the budget
    hang      blocked with no timeout, or over the latency budget
    miss      made a request the original never made
    """
    note = None
    if r["outcome"] == "hang" or r["latency"] > budget + 1e-9:
        cls = "hang"
    elif r["outcome"] == "miss":
        cls = "miss"
    elif r["outcome"] == "raise":
        cls = "ok" if "exc" in call and r["matches"] else "error"
    elif "exc" not in call and r["matches"]:
        cls = "ok"
    elif fallback_allowed and r["is_fallback"]:
        note = unearned(r, keys)
        cls = "wrong" if note else "degraded"
    else:
        cls = "wrong"
    return {**r, "class": cls, "note": note, "dups": duplicates(r["attempts"])}


def same_side_effects(o: dict, h: dict) -> bool:
    """Did the hardened call leave its arguments as the original did? (`self` may gain attributes.)"""
    a, b = o.get("state") or {}, h.get("state") or {}
    return a.get("args") == b.get("args") and all(
        (b.get("self") or {}).get(k) == v for k, v in (a.get("self") or {}).items())


def summarize(ts: list[dict]) -> dict:
    n = len(ts)
    c = {k: sum(1 for t in ts if t["class"] == k) for k in CLASSES}
    lat = sorted(t["latency"] for t in ts)
    failures = [t for t in ts if t["class"] in ("error", "degraded")]
    errors = [t for t in ts if t["class"] == "error"]
    return {
        "trials": n, **c,
        "availability": (c["ok"] + c["degraded"]) / n if n else 0.0,
        "p50": lat[n // 2] if n else None,
        "p99": lat[min(n - 1, int(0.99 * n))] if n else None,
        "max": lat[-1] if n else None,
        "dups": sum(t["dups"] for t in ts),
        "logged_failures": (sum(1 for t in failures if t["logs"]) / len(failures)) if failures else None,
        "structured_errors": (sum(1 for t in errors if t["structured"]) / len(errors)) if errors else None,
    }


def new_crashes(original: list[dict], hardened: list[dict], bug_types) -> list[str]:
    """Programming-error exception types the hardened code raised and the original never did."""
    known = {t["exc_type"] for t in original if t.get("exc_type")}
    found = set()
    for t in hardened:
        if t["class"] == "error" and t.get("exc_type"):
            et = t["cause_type"] if t["exc_type"] == "RasError" and t.get("cause_type") else t["exc_type"]
            if et in bug_types and et not in known:
                found.add(et)
    return sorted(found)


def gates(happy: dict, o: dict, h: dict, crashes: list, budget: float) -> dict:
    return {
        "happy": happy["total"] > 0 and happy["equal"] == happy["total"] and happy["original_nondeterministic"] == 0,
        "no_wrong": h["wrong"] == 0 and h["miss"] == 0,
        "no_hang": h["hang"] == 0,
        "no_new_crashes": not crashes,
        "p99": h["p99"] is not None and h["p99"] <= budget,
        "dups": h["dups"] <= o["dups"],
        "availability": h["availability"] > o["availability"],
    }


def classify_run(r, baseline: str, markers: list[str], fallbacks: int = 0, failures: int = 1) -> str:
    """One whole-app run. correct: identical output · degraded: output differs only where the app said
    so (a marker) or after a hardened function returned its *declared* fallback in this run, and a
    dependency was seen to fail in it · wrong: anything else."""
    if r.hang:
        return "hang"
    if r.code != 0:
        return "crash"
    if r.stdout == baseline:
        return "correct"
    if not failures:
        return "wrong"
    a, b = r.stdout.splitlines(), baseline.splitlines()
    if markers and len(a) == len(b):
        diff = [x for x, y in zip(a, b) if x != y]
        if all(any(m in x for m in markers) for x in diff):
            return "degraded"
    if fallbacks:
        return "degraded"
    return "wrong"
