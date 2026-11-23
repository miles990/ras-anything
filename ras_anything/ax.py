"""V-18 Agent Experience (AX): leak scanner, deterministic policy agent, and before/after measurement."""

from __future__ import annotations

import json
import re
from urllib.error import HTTPError

from . import runtime as rt

_ACTION_FOR_FAULT = {
    "timeout": "retry_later",
    "hang": "retry_later",
    "slow": "retry_later",
    "refused": "retry_later",
    "unavailable": "retry_later",
    "http_503": "retry_later",
    "rate_limited": "retry_later",
    "http_429": "retry_later",
    "truncated": "retry_later",
    "circuit_open": "retry_later",
    "rejected": "retry_later",
    "outage": "retry_later",
    "down": "retry_later",
    "invalid_request": "change_params",
    "auth": "escalate",
    "quota": "escalate",
    "unsupported": "escalate",
    "dependency_bug": "escalate",
    "approval": "escalate",
    "approval_required": "escalate",
    "conflict": "stop",
    "bug": "stop",
    "unknown": "stop",
}

_LEAK_PATTERNS = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("token", re.compile(r"(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}|\beyJ[\w-]{8,}\.[\w-]+\.[\w-]*|\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|xox[abprs]|AKIA|ASIA|AIza)[-_A-Za-z0-9]{12,}", re.I)),
    ("secret_kv", re.compile(r"\b(?:api[_-]?key|access[_-]?key|token|secret|passw(?:or)?d|pwd|authorization)\s*[=:]\s*(?!\[secret\]|\[token\])[^\s\"'&,;}]+", re.I)),
    ("traceback", re.compile(r"Traceback \(most recent call last\):|\bFile \"[^\"]+\", line \d+|\n\s+at \S+")),
    ("url", re.compile(r"\bhttps?://[^\s\"'<>)\]]+", re.I)),
    ("ip", re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?![\w.])")),
    ("localhost", re.compile(r"\blocalhost(?::\d+)?\b")),
    ("path", re.compile(r"(?<![\w.:])(?:/(?:home|etc|usr|var|tmp|opt|srv|workspace|Users)/[\w.@+/-]+|[A-Za-z]:\\\\?[^\s\"']+)")),
)


def expected_action(fault: str | None, degraded: bool = False) -> str:
    if degraded:
        return "use_fallback"
    return _ACTION_FOR_FAULT.get((fault or "").lower(), "stop")


def scan_leaks(surface) -> list[str]:
    """Return names of any PII / secret / internal leaks found in an error surface (str or dict)."""
    text = json.dumps(surface, ensure_ascii=False) if isinstance(surface, dict) else str(surface or "")
    # OTel nanosecond timestamps and explicit local test server.address metadata are not PII/card leaks
    text_for_num = re.sub(r'"Timestamp":\s*"\d+"', '"Timestamp": "[ts]"', text)
    text_for_ip = re.sub(r'"server\.address":\s*"(?:127\.0\.0\.1|::1)"', '"server.address": "[loopback]"', text)
    hits = []
    for name, rx in _LEAK_PATTERNS:
        tgt = text_for_ip if name == "ip" else text
        if rx.search(tgt):
            hits.append(name)
    for m in rt._CARD.finditer(text_for_num):
        if rt._luhn(m.group()):
            hits.append("card")
            break
    if rt._PHONE.search(text_for_num):
        hits.append("phone")
    return sorted(set(hits))


def policy_agent(surface) -> dict:
    """Deterministic policy agent reading ONLY the error surface and picking {action, tokens, steps}."""
    if surface is None or surface == "":
        return {"action": None, "tokens": 1, "steps": 3, "needs_human": False}

    if isinstance(surface, dict):
        txt = json.dumps(surface, ensure_ascii=False)
        tok = max(1, len(txt) // 4)
        act = surface.get("next_action")
        if act in ("retry_later", "change_params", "use_fallback", "escalate", "stop"):
            return {
                "action": act,
                "tokens": tok,
                "steps": 1,
                "retry_after_s": surface.get("retry_after_s"),
                "needs_human": bool(surface.get("needs_human_authorization")),
            }

    txt = str(surface)
    tok = max(1, len(txt) // 4)
    if txt.lstrip().startswith("{"):
        try:
            obj = json.loads(txt)
            if isinstance(obj, dict) and "next_action" in obj:
                return policy_agent(obj)
        except ValueError:
            pass

    m_next = re.search(r"\bnext:\s*(retry_later|change_params|use_fallback|escalate|stop)\b", txt)
    if m_next:
        return {
            "action": m_next.group(1),
            "tokens": tok,
            "steps": 1,
            "needs_human": "approval_required" in txt,
        }

    # Unstructured / generic original error text: agent must guess from raw exception/traceback
    low = txt.lower()
    if any(w in low for w in ("timed out", "timeouterror", "etimedout", "503", "502", "504", "econnrefused", "econnreset", "429", "rate limit", "too many requests")):
        return {"action": "retry_later", "tokens": tok, "steps": 2, "needs_human": False}
    if any(w in low for w in ("400", "404", "422", "bad request", "invalid")):
        return {"action": "change_params", "tokens": tok, "steps": 2, "needs_human": False}
    if any(w in low for w in ("401", "403", "unauthorized", "forbidden", "approval")):
        return {"action": "escalate", "tokens": tok, "steps": 2, "needs_human": "approval" in low}
    # Note: generic "fetch failed", "HTTP Error 402", "HTTP Error 501", "HTTP Error 409", bare tracebacks
    # cannot be disambiguated reliably -> 3 steps & often wrong action ("stop")
    return {"action": "stop", "tokens": tok, "steps": 3, "needs_human": False}


def evaluate_surfaces(cases: list[dict]) -> dict:
    """Evaluate a list of {original, hardened, expected, fault} items -> before/after AX metrics."""
    if not cases:
        return {"cases": 0, "original": None, "hardened": None}

    out = {}
    for side in ("original", "hardened"):
        correct = 0
        tokens = []
        steps = []
        leaks = 0
        for c in cases:
            surf = c.get(side)
            res = policy_agent(surf)
            if res["action"] == c["expected"]:
                correct += 1
            tokens.append(res["tokens"])
            steps.append(res["steps"])
            if surf:
                leaks += len(scan_leaks(surf))
        n = len(cases)
        out[side] = {
            "cases": n,
            "accuracy": correct / n,
            "mean_tokens": round(sum(tokens) / n, 1),
            "mean_steps": round(sum(steps) / n, 2),
            "leaks": leaks,
        }
    return {"cases": len(cases), **out}


def catalog_benchmark() -> dict:
    """Deterministic AX comparison across the full RFC 9457 / V-18 fault catalog (no network)."""
    from email.message import Message
    import io

    def _http(code, retry_after=None):
        h = Message()
        if retry_after is not None:
            h["Retry-After"] = str(retry_after)
        return HTTPError("http://10.1.2.3:8080/v1/charges", code, "err", h, io.BytesIO(b""))

    raw_cases = [
        ("timeout", TimeoutError("timed out calling http://10.1.2.3:8080/v1/charges"),
         'Traceback (most recent call last):\n  File "/srv/app/billing_handler", line 42, in charge\n    urlopen("http://10.1.2.3:8080/v1/charges")\nTypeError: fetch failed for bob@x.com'),
        ("unavailable", _http(503),
         'Traceback (most recent call last):\n  File "/srv/app/billing_handler", line 42, in charge\n    raise Exception("upstream error on http://10.1.2.3:8080/v1/charges for token=sk-live-1234567890abcdef")'),
        ("rate_limited", _http(429, "5"),
         'Traceback (most recent call last):\n  File "/srv/app/billing_handler", line 42, in charge\n    raise Exception("blocked by http://10.1.2.3:8080/v1/charges")'),
        ("quota", _http(402),
         'Traceback (most recent call last):\n  File "/srv/app/billing_handler", line 42, in charge\n    urllib.error.HTTPError: HTTP Error 402: Payment Required at http://10.1.2.3:8080/v1/charges'),
        ("invalid_request", _http(422),
         'Traceback (most recent call last):\n  File "/srv/app/billing_handler", line 42, in charge\n    raise ValueError("rejected payload from http://10.1.2.3:8080/v1/charges")'),
        ("auth", _http(403),
         'Traceback (most recent call last):\n  File "/srv/app/billing_handler", line 42, in charge\n    urllib.error.HTTPError: HTTP Error 403: Forbidden at http://10.1.2.3:8080/v1/charges'),
        ("conflict", _http(409),
         'Traceback (most recent call last):\n  File "/srv/app/billing_handler", line 42, in charge\n    urllib.error.HTTPError: HTTP Error 409: Conflict at http://10.1.2.3:8080/v1/charges'),
        ("dependency_bug", _http(501),
         'Traceback (most recent call last):\n  File "/srv/app/billing_handler", line 42, in charge\n    urllib.error.HTTPError: HTTP Error 501: Not Implemented at http://10.1.2.3:8080/v1/charges'),
        ("approval_required", rt._lazy("problem", "ApprovalRequired")("charge", {"email": "bob@x.com"}),
         'Traceback (most recent call last):\n  File "/srv/app/billing_handler", line 42, in charge\n    PermissionError: operation denied for bob@x.com'),
        ("unknown", RuntimeError("unexpected failure at /etc/secrets/key.pem"),
         'Traceback (most recent call last):\n  File "/srv/app/billing_handler", line 42, in charge\n    RuntimeError: unexpected failure at /etc/secrets/key.pem'),
    ]
    cases = []
    with rt.trace():
        for cause, exc, orig_surface in raw_cases:
            err = exc if isinstance(exc, rt.RasError) else rt.RasError(
                "request to bob@x.com via http://10.1.2.3:8080/v1/charges failed",
                cause=exc,
                dependency="charges",
            )
            cases.append({
                "fault": cause,
                "expected": expected_action(cause),
                "original": orig_surface,
                "hardened": err.problem,
            })
    return evaluate_surfaces(cases)
