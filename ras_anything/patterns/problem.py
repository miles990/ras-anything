"""Errors an agent can act on: RFC 9457 problem details for any failure, and the human gate for irreversible operations.

Not a pattern of a Policy: what is left to say when all of them have given up. The same members and the same table are
in the Node.js runtime (lang/node_agent/patterns/problem.cjs); every language runtime must produce this shape.
"""

from __future__ import annotations

import json
import os

from .. import runtime as rt

# cause -> title, status, retryable, next_action
CAUSES = {
    "timeout": ("The dependency did not answer in time", 504, True, "retry_later"),
    "unavailable": ("The dependency is unavailable", 503, True, "retry_later"),
    "rate_limited": ("The dependency asks for fewer requests", 429, True, "retry_later"),
    "quota": ("A quota or a budget is used up", 429, False, "escalate"),
    "invalid_request": ("The request was refused as invalid", 400, False, "change_params"),
    "auth": ("Not authorized at the dependency", 403, False, "escalate"),
    "conflict": ("The request conflicts with the current state", 409, False, "stop"),
    "dependency_bug": ("The dependency answered in a way that can't be used", 502, False, "escalate"),
    "approval_required": ("A person has to authorize this operation", 403, False, "escalate"),
    "unknown": ("The operation failed", 500, False, "stop"),
}
NEXT_ACTIONS = ("retry_later", "change_params", "use_fallback", "escalate", "stop")
FAULT_CAUSE = {"timeout": "timeout", "refused": "unavailable", "unavailable": "unavailable", "truncated": "unavailable", "circuit_open": "unavailable",
               "rejected": "unavailable", "rate_limited": "rate_limited", "quota": "quota", "invalid_request": "invalid_request", "auth": "auth",
               "conflict": "conflict", "unsupported": "dependency_bug", "approval": "approval_required"}


def problem(exc: BaseException) -> dict:
    """What an agent needs to know about a failure, and nothing about our insides: RFC 9457 members (type, title,
    status, detail, instance) plus cause, dependency (a logical name), retryable, retry_after_s, next_action,
    needs_human_authorization, correlation_id."""
    advice = dict(getattr(exc, "advice", None) or {})
    cause = advice.get("category") or FAULT_CAUSE.get(rt.fault_class(exc), "unknown")
    if cause not in CAUSES:
        cause = "unknown"
    title, status, retryable, action = CAUSES[cause]
    root = exc
    while getattr(root, "cause", None) is not None and isinstance(root.cause, BaseException):
        root = root.cause
    real = rt._http_status(root)
    if cause in ("invalid_request", "auth", "conflict") and real:
        status = real
    wait = advice.get("retry_after_s")
    if wait is None:
        wait = rt._retry_after(root)
    if wait is None and cause in ("timeout", "unavailable", "rate_limited"):
        inner = getattr(root, "advice", None) or {}
        wait = inner.get("retry_after_s")
    dep = advice.get("dependency") or (getattr(root, "advice", None) or {}).get("dependency")
    if dep is None:
        ctx = rt._ctx.get()
        dep = (ctx.last_dependency or (ctx.deps[0]["name"] if len(ctx.deps) == 1 else None)) if ctx is not None else None
    tid = getattr(exc, "trace_id", None) or rt.trace_id()
    retryable = bool(advice.get("retryable", retryable))
    action = advice.get("next_action") or ("retry_later" if retryable else "stop" if action == "retry_later" else action)
    human = bool(advice.get("needs_human_authorization", cause == "approval_required"))
    detail = exc.message if isinstance(exc, rt.RasError) else "%s" % title
    return {
        "type": "urn:ras-anything:problem:" + cause, "title": title, "status": int(advice.get("status") or status),
        "detail": rt.plain(detail), "instance": "urn:trace:" + tid, "cause": cause,
        "dependency": rt.plain(dep, 60) if dep else None, "retryable": retryable,
        "retry_after_s": round(float(wait), 3) if wait is not None else None,
        "next_action": action if action in NEXT_ACTIONS else "stop", "needs_human_authorization": human, "correlation_id": tid,
    }


# --------------------------------------------------------------------------- human gate


class ApprovalRequired(rt.RasError):
    """An irreversible operation nobody with the authority has approved."""

    def __init__(self, op: str, details: dict | None = None, message: str | None = None, **kw):
        super().__init__(message or "a person has to approve %s first" % op, context={"op": op, **(details or {})},
                         category="approval_required", retryable=False, next_action="escalate", needs_human_authorization=True, **kw)
        self.op = op
        self.needs_human_authorization = True
        self.next_action = "escalate"


_approver = None


def set_approver(fn):
    """`fn(op, details) -> bool` decides from now on (None: back to the environment and the file). Returns the one before."""
    global _approver
    prev, _approver = _approver, fn
    return prev


def require_approval(op: str, details: dict | None = None) -> bool:
    """Call it before an operation that can't be undone (a charge, a delete, a message to a person). True if it may
    go on, else ApprovalRequired: needs_human_authorization, next_action "escalate".

    Who decides: the approver (set_approver); else $RAS_ANYTHING_APPROVE, a comma-separated list of operations or
    "*"; else the JSON file $RAS_ANYTHING_APPROVALS (default .ras/approvals.json): {"charge": true} or
    {"charge": {"max": {"amount_cents": 5000}}}. Nobody answering, an approver that fails, or a file that can't be
    read is a no: it fails closed."""
    details = dict(details or {})
    ok, by = False, "nobody"
    try:
        if _approver is not None:
            ok, by = _approver(op, details) is True, "approver"
        else:
            env = [x.strip() for x in os.environ.get("RAS_ANYTHING_APPROVE", "").split(",") if x.strip()]
            if "*" in env or op in env:
                ok, by = True, "environment"
            else:
                ok, by = _file_says(op, details), "file"
    except Exception as e:
        rt.log_event("ras.approval.error", op=op, error=e)
        ok = False
    if ok:
        rt.log_event("ras.approval.granted", op=op, by=by)
        return True
    rt.log_event("ras.approval.denied", op=op, **{"ras.next_action": "escalate", "ras.needs_human_authorization": True})
    raise ApprovalRequired(op, details)


def _file_says(op, details) -> bool:
    path = os.environ.get("RAS_ANYTHING_APPROVALS") or os.path.join(".ras", "approvals.json")
    try:
        with open(path) as f:
            rule = json.load(f).get(op)
    except FileNotFoundError:
        return False
    if rule is True:
        return True
    limits = rule.get("max") if isinstance(rule, dict) else None
    if not isinstance(limits, dict) or not limits:
        return False
    return all(isinstance(details.get(k), (int, float)) and not isinstance(details.get(k), bool) and details[k] <= v for k, v in limits.items())
