"""Canary routing, shadow mode, fail-safe swap, isolation shards, blast-radius helpers.

The marked enable block (Python and Node) hands the original and the hardened function to
`swap(id, original, hardened)`. Every call then:

- `RAS_ANYTHING_DISABLE=1` or `ras.enable.<id>` at 0 → original
- `--shadow` → serve the original; optionally run hardened with side effects suppressed
- `--canary N` → sticky-hash the targeting key; N% of keys get the hardened function
- a bug in the router or flag evaluation itself → original (fail-safe)

Isolation: `Policy.shard(key)` (see patterns/policy.py) gives a key its own breaker/budget.
"""

from __future__ import annotations

from . import flags, runtime


def fail_safe(fn, fallback, *args, **kwargs):
    """Run `fn`; on any unexpected exception, return `fallback()` (or `fallback` if not callable)."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        runtime.log_event("ras.fail_safe", error=type(e).__name__, where=getattr(fn, "__name__", "fn"))
        return fallback(*args, **kwargs) if callable(fallback) else fallback


# route / swap / set_shadow_suppressor: the runtime's (bottom of this file) are the only implementation.


# --------------------------------------------------------------------------- canary analysis (promote / rollback)


def analyse(window: dict, *, min_calls: int = 20) -> dict:
    """Decide promote / rollback / wait from a window of original vs canary SLIs.

    `window` = {"original": {availability, p99, error_rate, dups, n}, "canary": {...}, "percent": int, "plan": [5,25,...]}.
    A canary is rolled back if availability drops more than 2 points, p99 grows >20% with no availability gain,
    dups appear, or error_rate doubles above 1%. Otherwise, if `n` is enough, promote to the next step (or 100).
    """
    o, c = window["original"], window["canary"]
    reasons = []
    if c.get("dups", 0) > o.get("dups", 0):
        reasons.append("canary added duplicate side effects (%s → %s)" % (o.get("dups", 0), c.get("dups", 0)))
    if o.get("availability", 1) - c.get("availability", 1) > 0.02:
        reasons.append("availability dropped more than 2 points (%.1f%% → %.1f%%)" % (100 * o.get("availability", 1), 100 * c.get("availability", 1)))
    if c.get("error_rate", 0) > max(0.01, 2 * o.get("error_rate", 0)) and c.get("error_rate", 0) - o.get("error_rate", 0) > 0.01:
        reasons.append("error rate rose (%.2f%% → %.2f%%)" % (100 * o.get("error_rate", 0), 100 * c.get("error_rate", 0)))
    if o.get("p99", 0) and c.get("p99", 0) > 1.2 * o["p99"] and c.get("availability", 1) <= o.get("availability", 1) + 0.005:
        reasons.append("p99 grew more than 20%% without an availability gain (%.3fs → %.3fs)" % (o["p99"], c["p99"]))
    n = int(c.get("n") or 0)
    if reasons:
        return {"decision": "rollback", "percent": 0, "reasons": reasons, "n": n}
    if n < min_calls:
        return {"decision": "wait", "percent": int(window.get("percent") or 0), "reasons": ["need %d canary calls, have %d" % (min_calls, n)], "n": n}
    plan = list(window.get("plan") or [5, 25, 50, 100])
    cur = int(window.get("percent") or 0)
    nxt = next((p for p in plan if p > cur), 100)
    return {"decision": "promote", "percent": nxt, "reasons": ["SLIs hold over %d calls" % n], "n": n}


def apply_decision(fid: str, decision: dict) -> dict:
    """Write the canary percent from an analyse() result (0 on rollback). Logs the reason."""
    pct = int(decision.get("percent") or 0)
    flags.store().set_flag(flags.enable_key(fid), pct)
    runtime.log_event("ras.canary." + decision["decision"], id=fid, percent=pct, reason="; ".join(decision.get("reasons") or []))
    return decision


# --------------------------------------------------------------------------- blast radius


def blast_radius(affected: int, total: int, contained_s: float | None = None) -> dict:
    """Share of requests touched by an incident, and (optional) time to automatic containment."""
    if total <= 0:
        return {"affected": 0, "total": 0, "share": None, "contained_s": contained_s}
    return {"affected": affected, "total": total, "share": affected / total, "contained_s": contained_s}


# The human gate is part of the runtime (patterns/problem.py); these names stay importable from here.
from .patterns.problem import ApprovalRequired as NeedsApproval  # noqa: E402,F401
from .patterns.problem import require_approval, set_approver  # noqa: E402,F401

# The runtime (vendored with enabled apps) is the implementation.
swap = runtime.swap
set_shadow_suppressor = runtime.set_shadow_suppressor
route = runtime.route
