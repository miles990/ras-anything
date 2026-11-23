"""The static reliability signals every language adapter reports, and how much each one weighs."""

from __future__ import annotations

# weight: how much each signal alone says "this will hurt under faults"
SIGNALS = {
    "no_timeout": (0.40, "{call}() without timeout"),
    "unsafe_retry": (0.40, "retries a {verb} with no Idempotency-Key"),
    "swallow": (0.30, "`{handler}` swallows errors"),
    "no_retry": (0.20, "no retry on transient errors"),
    "write_no_key": (0.15, "{verb} without Idempotency-Key"),
    "no_context": (0.10, "re-raises without `from`/context"),
    "open_no_with": (0.10, "open() outside `with`"),
    "no_logging": (0.05, "no logging on the failure path"),
}


def risk(found) -> float:
    """Independent signals combined into one number between 0 and 1."""
    keep = 1.0
    for k in found:
        keep *= 1.0 - SIGNALS[k][0]
    return round(1.0 - keep, 4)


def by_weight(found: dict) -> list[str]:
    return [txt for _, txt in sorted(found.items(), key=lambda kv: -SIGNALS[kv[0]][0])]
