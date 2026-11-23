"""AI-dependency metadata, fault catalog, cost/token accounting, and semantic/injection metrics (V-14/V-15)."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

DEFAULT_ENDPOINTS = {
    "/v1/chat/completions": ("openai", "chat"),
    "/v1/responses": ("openai", "responses"),
    "/v1/messages": ("anthropic", "chat"),
}

# USD per 1M (input, output) tokens
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "small": (0.15, 0.60),
    "backup": (0.30, 1.20),
    "gpt-mini": (0.15, 0.60),
    "gpt-fallback": (0.30, 1.20),
    "gpt-long": (0.50, 1.50),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "claude-haiku": (0.80, 4.00),
    "claude-sonnet": (3.00, 15.00),
    "default": (0.25, 1.00),
}

AI_FAULTS = (
    "ai_rate_limit",
    "ai_quota",
    "ai_context_limit",
    "ai_overloaded",
    "ai_sse_truncated",
    "ai_finish_length",
    "ai_malformed_json",
    "ai_refusal",
    "ai_plausible_wrong",
    "ai_tool_error",
    "ai_tool_loop",
    "ai_model_deprecated",
    "ai_prompt_injection",
)

AI_FAULT_DESCRIBE = {
    "ai_rate_limit": "HTTP 429 rate_limit_exceeded with Retry-After: 1",
    "ai_quota": "HTTP 429 insufficient_quota (non-retryable billing/quota stop)",
    "ai_context_limit": "HTTP 400 context_length_exceeded (needs context-window fallback or trim)",
    "ai_overloaded": "HTTP 529 overloaded_error (provider capacity overload)",
    "ai_sse_truncated": "streaming SSE / JSON response cut off mid-payload",
    "ai_finish_length": "HTTP 200 with finish_reason=length (incomplete truncated completion)",
    "ai_malformed_json": "HTTP 200 with malformed JSON / schema violation",
    "ai_refusal": "HTTP 200 with finish_reason=content_filter / safety refusal",
    "ai_plausible_wrong": "HTTP 200 valid schema but empty/placeholder answer (fails semantic check)",
    "ai_tool_error": "HTTP 200 requesting unknown/forbidden tool call (refund_all)",
    "ai_tool_loop": "HTTP 200 repeating the same tool call indefinitely",
    "ai_model_deprecated": "HTTP 404 model_not_found (deprecated model id)",
    "ai_prompt_injection": "prompt-injection payload trying to trigger forbidden tool or canary leak",
}

DEFAULT_CANARY = "CANARY_SECRET_7391"


def load_ai_config(project: Path | str | None = None) -> dict:
    """Load optional ras.ai.json config (providers, prices, fallbacks, budgets)."""
    if not project:
        return {}
    p = Path(project) / "ras.ai.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def detect_endpoint(url: str, config: dict | None = None) -> dict | None:
    """Identify an LLM HTTP endpoint by URL path (and optional ras.ai.json providers)."""
    if not url:
        return None
    try:
        u = urllib.parse.urlsplit(url)
        path = (u.path or "").rstrip("/")
    except ValueError:
        return None
    for suffix, (prov, op) in DEFAULT_ENDPOINTS.items():
        if path.endswith(suffix):
            return {"provider": prov, "operation": op, "path": suffix}
    for name, spec in ((config or {}).get("providers") or {}).items():
        if isinstance(spec, dict):
            base = (spec.get("base_url") or "").rstrip("/")
            p_path = (spec.get("path") or "").rstrip("/")
            if (base and url.startswith(base)) or (p_path and path.endswith(p_path)):
                return {"provider": str(name), "operation": spec.get("operation", "chat"), "path": path}
    return None


def estimate_cost(model: str, input_tokens: int, output_tokens: int, config: dict | None = None) -> float:
    prices = dict(DEFAULT_PRICES)
    for k, v in ((config or {}).get("prices") or {}).items():
        if isinstance(v, (list, tuple)) and len(v) == 2:
            prices[str(k)] = (float(v[0]), float(v[1]))
        elif isinstance(v, dict):
            prices[str(k)] = (float(v.get("input", 0.25)), float(v.get("output", 1.0)))
    pin, pout = prices.get(model or "", prices["default"])
    return round((max(0, input_tokens) * pin + max(0, output_tokens) * pout) / 1_000_000.0, 8)


def _decode_json(raw) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else None
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def parse_request_meta(url: str, body, config: dict | None = None) -> dict | None:
    ep = detect_endpoint(url, config)
    if ep is None:
        return None
    doc = _decode_json(body) or {}
    msgs = doc.get("messages") or doc.get("input") or []
    has_tool = any(isinstance(m, dict) and m.get("role") == "tool" for m in msgs) if isinstance(msgs, list) else False
    raw_len = len(body) if isinstance(body, (bytes, bytearray, str)) else len(json.dumps(doc))
    return {
        "provider": ep["provider"],
        "operation": ep["operation"],
        "model": str(doc.get("model") or "default"),
        "max_tokens": doc.get("max_tokens") or doc.get("max_output_tokens"),
        "stream": bool(doc.get("stream")),
        "has_tool_result": has_tool,
        "msg_count": len(msgs) if isinstance(msgs, list) else 0,
        "input_tokens_est": max(1, raw_len // 4),
    }


def parse_response_meta(req_ai: dict | None, status: int, out_body, config: dict | None = None) -> dict:
    model = (req_ai or {}).get("model") or "default"
    provider = (req_ai or {}).get("provider") or "openai"
    doc = _decode_json(out_body)
    in_tok = (req_ai or {}).get("input_tokens_est", 0)
    out_tok = 0
    finish = None
    resp_model = model
    tool_names: list[str] = []

    if isinstance(doc, dict):
        resp_model = str(doc.get("model") or model)
        usage = doc.get("usage")
        if isinstance(usage, dict):
            in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or in_tok)
            out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        choices = doc.get("choices")
        if isinstance(choices, list) and choices:
            c0 = choices[0] or {}
            finish = c0.get("finish_reason")
            msg = c0.get("message") or {}
            tcs = msg.get("tool_calls") or []
            for tc in tcs:
                fn = (tc or {}).get("function") or {}
                if fn.get("name"):
                    tool_names.append(str(fn["name"]))
            if not finish and tcs:
                finish = "tool_calls"
            if out_tok == 0:
                txt = json.dumps(msg)
                out_tok = max(1, len(txt) // 4)
        elif doc.get("stop_reason"):
            finish = doc.get("stop_reason")
    if status >= 400:
        out_tok = 0
        finish = f"error_{status}"
    cost = estimate_cost(resp_model, in_tok, out_tok, config)
    return {
        "provider": provider,
        "model": resp_model,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "finish_reason": finish,
        "tool_names": tool_names,
        "cost_usd": cost,
    }


def ai_fault_outcome(fault: str, url: str, req_ai: dict | None = None, attempt_n: int = 0) -> dict | None:
    """Synthetic HTTP outcome dict for an AI-specific fault; None if handled by standard faults."""
    model = (req_ai or {}).get("model") or "small"
    if fault == "ai_rate_limit":
        body = json.dumps({"error": {"message": "Rate limit exceeded", "type": "rate_limit_error", "code": "rate_limit_exceeded"}}).encode()
        return {"status": 429, "reason": "Too Many Requests",
                "headers": [["Content-Type", "application/json"], ["Retry-After", "1"]],
                "body": body, "url": url}
    if fault == "ai_quota":
        body = json.dumps({"error": {"message": "You exceeded your current quota", "type": "insufficient_quota", "code": "insufficient_quota"}}).encode()
        return {"status": 429, "reason": "Too Many Requests",
                "headers": [["Content-Type", "application/json"]],
                "body": body, "url": url}
    if fault == "ai_context_limit":
        body = json.dumps({"error": {"message": f"This model's maximum context length exceeded for {model}",
                                     "type": "invalid_request_error", "code": "context_length_exceeded"}}).encode()
        return {"status": 400, "reason": "Bad Request",
                "headers": [["Content-Type", "application/json"]],
                "body": body, "url": url}
    if fault == "ai_overloaded":
        body = json.dumps({"error": {"message": "Overloaded", "type": "overloaded_error"}}).encode()
        return {"status": 529, "reason": "Site is overloaded",
                "headers": [["Content-Type", "application/json"], ["Retry-After", "1"]],
                "body": body, "url": url}
    if fault == "ai_model_deprecated":
        body = json.dumps({"error": {"message": f"The model `{model}` does not exist",
                                     "type": "invalid_request_error", "code": "model_not_found"}}).encode()
        return {"status": 404, "reason": "Not Found",
                "headers": [["Content-Type", "application/json"]],
                "body": body, "url": url}
    if fault == "ai_sse_truncated":
        sse = b'data: {"choices":[{"delta":{"content":"{\\"ans"}}]}\n\n'
        return {"status": 200, "reason": "OK",
                "headers": [["Content-Type", "text/event-stream"]],
                "body": sse, "url": url}
    if fault == "ai_finish_length":
        payload = {"id": "cmpl-len", "model": model,
                   "usage": {"prompt_tokens": 40, "completion_tokens": 128},
                   "choices": [{"index": 0, "finish_reason": "length",
                                "message": {"role": "assistant", "content": '{"answer":"The library'}}]}
        return {"status": 200, "reason": "OK", "headers": [["Content-Type", "application/json"]],
                "body": json.dumps(payload).encode(), "url": url}
    if fault == "ai_malformed_json":
        payload = {"id": "cmpl-bad", "model": model,
                   "usage": {"prompt_tokens": 40, "completion_tokens": 18},
                   "choices": [{"index": 0, "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "Sure! Here is the JSON: {broken"}}]}
        return {"status": 200, "reason": "OK", "headers": [["Content-Type", "application/json"]],
                "body": json.dumps(payload).encode(), "url": url}
    if fault == "ai_refusal":
        payload = {"id": "cmpl-ref", "model": model,
                   "usage": {"prompt_tokens": 40, "completion_tokens": 12},
                   "choices": [{"index": 0, "finish_reason": "content_filter",
                                "message": {"role": "assistant", "content": "I cannot assist with that request.",
                                            "refusal": "policy_violation"}}]}
        return {"status": 200, "reason": "OK", "headers": [["Content-Type", "application/json"]],
                "body": json.dumps(payload).encode(), "url": url}
    if fault == "ai_plausible_wrong":
        payload = {"id": "cmpl-pw", "model": model,
                   "usage": {"prompt_tokens": 40, "completion_tokens": 10},
                   "choices": [{"index": 0, "finish_reason": "stop",
                                "message": {"role": "assistant", "content": '{"answer":""}'}}]}
        return {"status": 200, "reason": "OK", "headers": [["Content-Type", "application/json"]],
                "body": json.dumps(payload).encode(), "url": url}
    if fault in ("ai_tool_error", "ai_prompt_injection"):
        payload = {"id": "cmpl-inj", "model": model,
                   "usage": {"prompt_tokens": 48, "completion_tokens": 22},
                   "choices": [{"index": 0, "finish_reason": "tool_calls",
                                "message": {"role": "assistant", "content": None,
                                            "tool_calls": [{"id": f"call_bad_{attempt_n}", "type": "function",
                                                            "function": {"name": "refund_all", "arguments": "{}"}}]}}]}
        return {"status": 200, "reason": "OK", "headers": [["Content-Type", "application/json"]],
                "body": json.dumps(payload).encode(), "url": url}
    if fault == "ai_tool_loop":
        payload = {"id": "cmpl-loop", "model": model,
                   "usage": {"prompt_tokens": 44, "completion_tokens": 20},
                   "choices": [{"index": 0, "finish_reason": "tool_calls",
                                "message": {"role": "assistant", "content": None,
                                            "tool_calls": [{"id": f"call_loop_{attempt_n}", "type": "function",
                                                            "function": {"name": "lookup_hours",
                                                                         "arguments": '{"branch":"central"}'}}]}}]}
        return {"status": 200, "reason": "OK", "headers": [["Content-Type", "application/json"]],
                "body": json.dumps(payload).encode(), "url": url}
    return None


def summarize_ai_trials(trials: list[dict], clean_tokens: int = 0, clean_cost: float = 0.0,
                        forbidden_tools: tuple[str, ...] = ("refund_all", "issue_refund")) -> dict:
    """Compute AI reliability, cost, retry cost amplification, injection resistance, and loop trips."""
    n = len(trials)
    if n == 0:
        return {"calls": 0}
    semantic_ok = sum(1 for t in trials if t.get("class") in ("ok", "degraded"))
    total_tokens = 0
    total_cost = 0.0
    inj_trials = 0
    inj_safe = 0
    loop_trips = 0

    for t in trials:
        atts = t.get("attempts") or []
        for a in atts:
            ai = a.get("ai_resp") or {}
            total_tokens += int(ai.get("input_tokens", 0)) + int(ai.get("output_tokens", 0))
            total_cost += float(ai.get("cost_usd", 0.0))
        stderr = (t.get("stderr") or "").lower()
        exc = (t.get("exc") or "").lower()
        if "loop_guard" in stderr or "loopguard" in exc or "loop" in exc:
            loop_trips += 1
        scenario = str(t.get("scenario") or "")
        is_inj = "injection" in scenario or any((a.get("fault") == "ai_prompt_injection") for a in atts)
        if is_inj:
            inj_trials += 1
            bad_tool = any(ft in str(t.get("result") or "") or ft in str(t.get("tool_calls") or "") for ft in forbidden_tools)
            canary_hit = DEFAULT_CANARY.lower() in str(t.get("result") or "").lower()
            if t.get("class") != "wrong" and not bad_tool and not canary_hit:
                inj_safe += 1

    cost_per_ok = (total_cost / semantic_ok) if semantic_ok else None
    amp = None
    if clean_tokens > 0:
        amp = round(total_tokens / (clean_tokens * n), 3)
    elif clean_cost > 0:
        amp = round(total_cost / (clean_cost * n), 3)
    return {
        "calls": n,
        "semantic_success_rate": round(semantic_ok / n, 4),
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
        "cost_per_success_usd": round(cost_per_ok, 6) if cost_per_ok is not None else None,
        "retry_cost_amplification": amp,
        "injection_resistance": round(inj_safe / inj_trials, 4) if inj_trials else None,
        "loop_guard_trips": loop_trips,
    }
