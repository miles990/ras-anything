"""AI-dependency resilience helpers (V-14 / V-15). Standard library only.

Vendored alongside runtime.py on `ras-anything enable`. Uses time.monotonic / time.sleep at
call time so verify's virtual clock runs deadlines, caches and Retry-After waits deterministically.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.error
from pathlib import Path

from .. import runtime

DEFAULT_PRICES = {
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


class SchemaValidationError(ValueError):
    """LLM output failed JSON parsing or schema validation."""


class ToolNotAllowedError(PermissionError):
    """Model or prompt injection attempted to invoke a disallowed or invalid tool."""


class LoopGuardError(RuntimeError):
    """Agent tool loop exceeded step or repeat bounds."""


class BudgetExceededError(RuntimeError):
    """Token, request, or cost budget exceeded."""


def parse_json_response(raw) -> dict | list:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        raise SchemaValidationError("expected JSON string or object, got %s" % type(raw).__name__)
    s = raw.strip()
    if not s:
        raise SchemaValidationError("empty completion content")
    try:
        return json.loads(s)
    except ValueError as e:
        raise SchemaValidationError("malformed JSON completion: %s" % e) from e


def validate_schema(obj, schema: dict) -> object:
    """Validate `obj` (or JSON string) against a compact JSON-Schema subset; returns parsed value."""
    if isinstance(obj, str) and schema.get("type") != "string":
        obj = parse_json_response(obj)
    _check_schema(obj, schema, "$")
    return obj


def _check_schema(val, schema: dict, path: str) -> None:
    if not schema:
        return
    t = schema.get("type")
    if t == "object":
        if not isinstance(val, dict):
            raise SchemaValidationError("%s: expected object" % path)
        for req_k in schema.get("required") or ():
            if req_k not in val:
                raise SchemaValidationError("%s: missing required field %r" % (path, req_k))
        props = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extra = set(val) - set(props)
            if extra:
                raise SchemaValidationError("%s: unexpected fields %s" % (path, sorted(extra)))
        for k, sub in props.items():
            if k in val:
                _check_schema(val[k], sub, f"{path}.{k}")
    elif t == "array":
        if not isinstance(val, list):
            raise SchemaValidationError("%s: expected array" % path)
        if "minItems" in schema and len(val) < int(schema["minItems"]):
            raise SchemaValidationError("%s: too few items" % path)
        if "maxItems" in schema and len(val) > int(schema["maxItems"]):
            raise SchemaValidationError("%s: too many items" % path)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(val):
                _check_schema(item, item_schema, f"{path}[{idx}]")
    elif t == "string":
        if not isinstance(val, str):
            raise SchemaValidationError("%s: expected string" % path)
        if schema.get("nonEmpty") and not val.strip():
            raise SchemaValidationError("%s: empty string" % path)
        if "minLength" in schema and len(val.strip() if schema.get("nonEmpty", True) else val) < int(schema["minLength"]):
            raise SchemaValidationError("%s: string shorter than minLength" % path)
        if "maxLength" in schema and len(val) > int(schema["maxLength"]):
            raise SchemaValidationError("%s: string longer than maxLength" % path)
    elif t == "integer":
        if type(val) is not int:
            raise SchemaValidationError("%s: expected integer" % path)
    elif t == "number":
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise SchemaValidationError("%s: expected number" % path)
    elif t == "boolean":
        if not isinstance(val, bool):
            raise SchemaValidationError("%s: expected boolean" % path)
    if "enum" in schema and val not in schema["enum"]:
        raise SchemaValidationError("%s: value %r not in enum %r" % (path, val, schema["enum"]))


def frame_untrusted(text: str, label: str = "UNTRUSTED_DOCUMENT") -> str:
    """Wrap untrusted text so closing tags inside it cannot break out of the data fence."""
    safe_label = "".join(ch for ch in str(label) if ch.isalnum() or ch in ("_", "-")) or "UNTRUSTED_DATA"
    s = str(text or "")
    s = s.replace(f"</{safe_label}>", f"&lt;/{safe_label}&gt;")
    s = s.replace("<untrusted_data", "&lt;untrusted_data").replace("</untrusted_data>", "&lt;/untrusted_data&gt;")
    return f"<{safe_label}>\n{s}\n</{safe_label}>"


def check_injection_leaks(text: str, canary: str | None = None) -> bool:
    """Return True if `canary` leaked into `text`."""
    return bool(canary and text and canary in str(text))


class ToolAllowlist:
    """Strict tool allowlist + argument schema validator."""

    def __init__(self, allowed: dict[str, dict] | set[str] | tuple[str, ...],
                 forbidden: set[str] | tuple[str, ...] = ("refund_all", "issue_refund")):
        if isinstance(allowed, dict):
            self.schemas = dict(allowed)
        else:
            self.schemas = {str(name): {} for name in allowed}
        self.forbidden = set(forbidden)

    def check(self, name: str, args) -> dict:
        if name in self.forbidden or name not in self.schemas:
            runtime.log_event("ras.ai.tool_blocked", **{"ras.tool": str(name), "ras.reason": "not_allowed"})
            raise ToolNotAllowedError("tool %r is not allowed" % name)
        parsed = parse_json_response(args) if isinstance(args, str) else args
        if not isinstance(parsed, dict):
            runtime.log_event("ras.ai.tool_blocked", **{"ras.tool": str(name), "ras.reason": "invalid_args"})
            raise ToolNotAllowedError("tool %r arguments must be an object" % name)
        schema = self.schemas[name]
        if schema:
            try:
                validate_schema(parsed, schema)
            except SchemaValidationError as e:
                runtime.log_event("ras.ai.tool_blocked", **{"ras.tool": str(name), "ras.reason": str(e)})
                raise ToolNotAllowedError("tool %r args rejected: %s" % (name, e)) from e
        return parsed


class LoopGuard:
    """Caps total tool steps and repeated identical tool calls in one agent run."""

    def __init__(self, max_steps: int = 6, max_repeat: int = 2):
        self.max_steps = int(max_steps)
        self.max_repeat = int(max_repeat)
        self.steps = 0
        self._last_sig: str | None = None
        self._repeat_count = 0

    def reset(self) -> None:
        self.steps = 0
        self._last_sig = None
        self._repeat_count = 0

    def record(self, tool_name: str, args=None) -> None:
        self.steps += 1
        if self.steps > self.max_steps:
            runtime.log_event("ras.ai.loop_guard", **{"ras.tool": tool_name, "ras.reason": "max_steps",
                                                      "steps": self.steps, "max_steps": self.max_steps})
            raise LoopGuardError("tool loop exceeded max_steps=%d" % self.max_steps)
        sig = "%s:%s" % (tool_name, json.dumps(args or {}, sort_keys=True, default=str))
        if sig == self._last_sig:
            self._repeat_count += 1
        else:
            self._last_sig = sig
            self._repeat_count = 1
        if self._repeat_count > self.max_repeat:
            runtime.log_event("ras.ai.loop_guard", **{"ras.tool": tool_name, "ras.reason": "repeated_call",
                                                      "repeats": self._repeat_count})
            raise LoopGuardError("repeated identical tool call %r %d times" % (tool_name, self._repeat_count))


class TokenBudget:
    """Tracks requests, tokens, and USD cost against hard ceilings."""

    def __init__(self, max_tokens: int | None = None, max_cost_usd: float | None = None,
                 max_requests: int | None = None, prices: dict | None = None):
        self.max_tokens = max_tokens
        self.max_cost_usd = max_cost_usd
        self.max_requests = max_requests
        self.prices = dict(DEFAULT_PRICES)
        if prices:
            self.prices.update(prices)
        self.reset()

    def reset(self) -> None:
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def check_before(self, est_tokens: int = 0) -> None:
        if self.max_requests is not None and self.requests >= self.max_requests:
            raise BudgetExceededError("request budget exhausted (%d/%d)" % (self.requests, self.max_requests))
        if self.max_tokens is not None and self.total_tokens + max(0, est_tokens) > self.max_tokens:
            raise BudgetExceededError("token budget exhausted (%d/%d)" % (self.total_tokens, self.max_tokens))
        if self.max_cost_usd is not None and self.cost_usd >= self.max_cost_usd:
            raise BudgetExceededError("cost budget exhausted ($%.6f/$%.6f)" % (self.cost_usd, self.max_cost_usd))

    def charge(self, model: str, input_tokens: int, output_tokens: int) -> float:
        self.requests += 1
        self.input_tokens += max(0, int(input_tokens))
        self.output_tokens += max(0, int(output_tokens))
        pin, pout = self.prices.get(model or "", self.prices["default"])
        delta = (max(0, int(input_tokens)) * pin + max(0, int(output_tokens)) * pout) / 1_000_000.0
        self.cost_usd += delta
        if self.max_tokens is not None and self.total_tokens > self.max_tokens:
            raise BudgetExceededError("token budget exceeded (%d > %d)" % (self.total_tokens, self.max_tokens))
        if self.max_cost_usd is not None and self.cost_usd > self.max_cost_usd:
            raise BudgetExceededError("cost budget exceeded ($%.6f > $%.6f)" % (self.cost_usd, self.max_cost_usd))
        return delta


class ExactCache:
    """Deterministic exact-key TTL cache for identical AI prompts."""

    def __init__(self, ttl_s: float = 300.0, max_entries: int = 256):
        self.ttl_s = float(ttl_s)
        self.max_entries = int(max_entries)
        self._data: dict[str, tuple[float, object]] = {}
        runtime.track(self)

    def reset(self) -> None:
        self._data.clear()

    @staticmethod
    def key_for(model: str, payload) -> str:
        raw = json.dumps({"model": model, "payload": payload}, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str):
        hit = self._data.get(key)
        if hit is None:
            return None
        exp, val = hit
        if time.monotonic() > exp:
            del self._data[key]
            return None
        return val

    def put(self, key: str, val) -> None:
        if len(self._data) >= self.max_entries:
            oldest = next(iter(self._data))
            del self._data[oldest]
        self._data[key] = (time.monotonic() + self.ttl_s, val)


class StepJournal:
    """File-backed durable step journal: resumes completed steps at $0 / 0 tokens without Temporal."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in str(run_id))
        return self.dir / f"{safe}.json"

    def _load(self, run_id: str) -> dict:
        p = self._path(run_id)
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, run_id: str, data: dict) -> None:
        p = self._path(run_id)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, sort_keys=True)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def step(self, run_id: str, name: str, fn):
        state = self._load(run_id)
        if name in state:
            return state[name]
        val = fn()
        state[name] = val
        self._save(run_id, state)
        return val

    def clear(self, run_id: str) -> None:
        self._path(run_id).unlink(missing_ok=True)


def _classify_ai_http(status: int, body_text: str) -> tuple[str, bool]:
    """Return (fault_class, retry_same_model)."""
    low = (body_text or "").lower()
    if status == 429:
        if "insufficient_quota" in low or "quota" in low:
            return "ai_quota", False
        return "ai_rate_limit", True
    if status == 400 and ("context_length" in low or "maximum context" in low or "too many tokens" in low):
        return "ai_context_limit", False
    if status == 404 and ("model_not_found" in low or "does not exist" in low):
        return "ai_model_deprecated", False
    if status == 529 or status in (500, 502, 503, 504, 408, 425):
        return "ai_overloaded" if status == 529 else "http5xx", True
    return f"http_{status}", False


class AIPolicy:
    """Resilience wrapper for LLM calls: fallback chain, deadlines, budgets, schema validation, OTel logging."""

    def __init__(self, dependency: str = "llm", models: list[str] | tuple[str, ...] = ("small", "backup"),
                 *, context_window_fallbacks: list[str] | tuple[str, ...] = (),
                 content_policy_fallbacks: list[str] | tuple[str, ...] = (),
                 max_attempts_per_model: int = 2, per_attempt_timeout_s: float = 1.5,
                 deadline_s: float = 5.0, max_tokens: int | None = 1024,
                 max_cost_usd: float | None = None, max_requests: int | None = None,
                 schema: dict | None = None, cache: ExactCache | None = None,
                 provider: str = "openai"):
        self.dependency = dependency
        self.models = list(models)
        self.context_window_fallbacks = list(context_window_fallbacks)
        self.content_policy_fallbacks = list(content_policy_fallbacks)
        self.max_attempts_per_model = max(1, int(max_attempts_per_model))
        self.per_attempt_timeout_s = float(per_attempt_timeout_s)
        self.deadline_s = float(deadline_s)
        self.max_tokens = max_tokens
        self.max_cost_usd = max_cost_usd
        self.max_requests = max_requests
        self.schema = schema
        self.cache = cache
        self.provider = provider

    def run(self, invoke_model, *, cache_payload=None, schema: dict | None = None):
        """Call `invoke_model(model, timeout_s)` through fallback chain, budget, and optional schema validation."""
        active_schema = schema if schema is not None else self.schema
        budget = TokenBudget(max_tokens=self.max_tokens, max_cost_usd=self.max_cost_usd,
                             max_requests=self.max_requests)
        deadline = time.monotonic() + self.deadline_s
        queue = list(self.models)
        tried: set[str] = set()
        last_err: BaseException | None = None

        while queue:
            model = queue.pop(0)
            if model in tried:
                continue
            tried.add(model)

            ckey = ExactCache.key_for(model, cache_payload) if (self.cache is not None and cache_payload is not None) else None
            if ckey is not None:
                cached = self.cache.get(ckey)
                if cached is not None:
                    return cached

            for attempt in range(self.max_attempts_per_model):
                rem = deadline - time.monotonic()
                if rem <= 0:
                    last_err = runtime.DeadlineExceeded("ai deadline exceeded after %.2fs" % self.deadline_s)
                    break
                budget.check_before()
                tmo = min(self.per_attempt_timeout_s, rem)
                try:
                    raw = invoke_model(model, tmo)
                    out_val, meta = self._extract_and_validate(raw, model, active_schema)
                    budget.charge(meta["model"], meta["input_tokens"], meta["output_tokens"])
                    runtime.log_event("gen_ai.client.operation", **{
                        "gen_ai.operation.name": "chat",
                        "gen_ai.provider.name": self.provider,
                        "gen_ai.request.model": model,
                        "gen_ai.response.model": meta["model"],
                        "gen_ai.usage.input_tokens": meta["input_tokens"],
                        "gen_ai.usage.output_tokens": meta["output_tokens"],
                        "gen_ai.response.finish_reasons": [meta["finish_reason"]] if meta["finish_reason"] else [],
                        "ras.dependency": self.dependency,
                    })
                    if ckey is not None:
                        self.cache.put(ckey, out_val)
                    return out_val
                except BudgetExceededError:
                    raise
                except urllib.error.HTTPError as e:
                    body_txt = ""
                    try:
                        body_txt = (e.read() or b"").decode("utf-8", errors="replace")
                    except Exception:
                        pass
                    fclass, retry_same = _classify_ai_http(e.code, body_txt)
                    last_err = e
                    if fclass == "ai_context_limit" and self.context_window_fallbacks:
                        queue = [m for m in self.context_window_fallbacks if m not in tried] + queue
                        break
                    if not retry_same:
                        break
                    ra = e.headers.get("Retry-After") if e.headers else None
                    delay = float(ra) if ra and ra.replace(".", "", 1).isdigit() else min(0.5, 0.05 * (2 ** attempt))
                    if time.monotonic() + delay < deadline:
                        time.sleep(delay)
                except ToolNotAllowedError as e:
                    last_err = e
                    break
                except (SchemaValidationError, ValueError) as e:
                    last_err = e
                    msg = str(e).lower()
                    if ("content_filter" in msg or "refusal" in msg) and self.content_policy_fallbacks:
                        queue = [m for m in self.content_policy_fallbacks if m not in tried] + queue
                        break
                    # bounded single retry on malformed JSON/truncation before switching model
                    if attempt + 1 >= self.max_attempts_per_model:
                        break
                except Exception as e:
                    last_err = e
                    if not runtime.is_transient(e):
                        break
                    if time.monotonic() + 0.05 < deadline:
                        time.sleep(min(0.25, 0.05 * (2 ** attempt)))

        runtime.log_event("ras.dependency.gave_up", **{
            "ras.dependency": self.dependency,
            "ras.fault": "ai_exhausted",
            "gen_ai.provider.name": self.provider,
        })
        raise RuntimeError(f"{self.dependency} failed across fallback chain") from last_err

    @staticmethod
    def _extract_and_validate(raw, model: str, schema: dict | None):
        if isinstance(raw, dict) and "choices" in raw:
            choices = raw.get("choices") or []
            if not choices:
                raise SchemaValidationError("empty choices")
            c0 = choices[0] or {}
            finish = c0.get("finish_reason")
            msg = c0.get("message") or {}
            if finish == "content_filter" or msg.get("refusal"):
                raise SchemaValidationError("content_filter refusal")
            if finish == "length":
                raise SchemaValidationError("finish_reason=length truncated output")
            usage = raw.get("usage") or {}
            meta = {
                "model": str(raw.get("model") or model),
                "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
                "finish_reason": finish or "stop",
            }
            tcs = msg.get("tool_calls") or []
            if tcs:
                return {"tool_calls": tcs, "message": msg}, meta
            content = msg.get("content")
            val = validate_schema(content, schema) if schema else content
            return val, meta
        meta = {"model": model, "input_tokens": 0, "output_tokens": 0, "finish_reason": "stop"}
        val = validate_schema(raw, schema) if schema else raw
        return val, meta
