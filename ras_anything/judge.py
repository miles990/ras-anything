"""Turns raw facts from the agents into verdicts: what each trial was, the gates, the availability.

Everything here is arithmetic on JSON and the same for every language. An agent reports what a call
did (returned, raised, hung; equal to the recorded result or not; every dependency attempt it made);
deciding what that is worth happens here, outside the process that runs the code being judged.
"""

from __future__ import annotations

from urllib.parse import urlsplit

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
    attempts = sum(len(t["attempts"]) for t in ts)
    return {
        "trials": n, **c,
        "attempts": attempts, "amplification": attempts / n if n else None,
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


def dependency_of(key: str) -> str:
    """What a request key talks to: scheme and host, or the program."""
    words = key.split(" ")
    if words[0] in ("run", "CONNECT"):
        return " ".join(words[:3])
    u = urlsplit(words[1]) if len(words) > 1 else urlsplit("")
    return "%s://%s" % (u.scheme, u.netloc)


KNOWN_DOWN_S = 60.0  # how long a failed request is evidence that its dependency is down


def classify_sequence(raw: list[dict], calls: list[dict], keys: list[list[str]], budget: float, fallback_allowed: bool, gaps: list[float]) -> list[dict]:
    """classify_trial for calls made one after the other with the code's own state kept (an outage). Here a
    fallback may also be earned without a request: an open circuit is the point. The evidence is the agent's, from
    the calls before: the last thing it saw of every dependency of this call was a failure, recently."""
    out, seen, now = [], {}, 0.0  # seen: dependency -> (when, failed)
    for r, call, ks, gap in zip(raw, calls, keys, gaps):
        now += gap
        t = classify_trial(r, call, ks, budget, fallback_allowed)
        if t["note"] and not r["attempts"] and r["logs"]:
            known = [seen.get(dependency_of(k)) for k in ks]
            if known and all(x and x[1] and now - x[0] <= KNOWN_DOWN_S for x in known):
                t.update({"class": "degraded", "note": None})
        now += r["latency"]
        for a in r["attempts"]:
            seen[dependency_of(a["key"])] = (now, a["failed"])
        out.append({**t, "at": now - r["latency"]})
    return out


def summarize_outage(phases: list[str], ts: list[dict]) -> dict:
    """What one side did in the sequence healthy -> dependency down -> recovered."""
    part = {p: [t for t, q in zip(ts, phases) if q == p] for p in ("healthy", "down", "recovered")}
    n = {p: len(x) for p, x in part.items()}
    attempts = {p: sum(len(t["attempts"]) for t in x) for p, x in part.items()}
    fresh = next((i for i, t in enumerate(part["recovered"]) if t["class"] == "ok" and any(not a["failed"] for a in t["attempts"])), None)
    c = {k: sum(t["class"] == k for t in (ts if k in ("wrong", "miss", "hang") else part["down"])) for k in CLASSES}
    return {
        "calls": n["down"], "attempts": attempts["down"], **c,
        "amplification": attempts["down"] / n["down"] if n["down"] else None,
        "healthy_per_call": attempts["healthy"] / n["healthy"] if n["healthy"] else None,
        "max_latency": max((t["latency"] for t in part["down"]), default=None),
        "recovered_after_s": None if fresh is None else round(part["recovered"][fresh]["at"] - part["recovered"][0]["at"], 3),
        "dups": sum(t["dups"] for t in ts),
    }


def gates(happy: dict, o: dict, h: dict, crashes: list, budget: float, outage: dict | None = None) -> dict:
    oo, oh = (outage["original"], outage["hardened"]) if outage else ({}, {})
    return {
        "happy": happy["total"] > 0 and happy["equal"] == happy["total"] and happy["original_nondeterministic"] == 0,
        "no_wrong": h["wrong"] == 0 and h["miss"] == 0 and oh.get("wrong", 0) + oh.get("miss", 0) == 0,
        "no_hang": h["hang"] == 0 and oh.get("hang", 0) == 0,
        "no_new_crashes": not crashes,
        "p99": h["p99"] is not None and h["p99"] <= budget,
        "dups": h["dups"] <= o["dups"] and oh.get("dups", 0) <= oo.get("dups", 0),
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
