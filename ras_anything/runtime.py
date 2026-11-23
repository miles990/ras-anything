"""Reliability helpers for hardened code. Standard library only.

Hardened functions may import these:

    from ras_anything.runtime import Policy, retry, call_with_timeout, CircuitBreaker, RasError, log_event

This module is the core (clock, randomness, trace context, errors, diagnostics, health, deadlines, what
counts as transient). Each resilience pattern is a module of its own in ras_anything/patterns/ and is loaded
when a name of it is first used here (docs/PATTERNS.md).

Everything that sleeps or reads the clock looks up ``time.sleep`` / ``time.monotonic`` at call
time, so ``ras-anything verify`` can run your code on a virtual clock (hangs and backoff cost no
wall time and latency numbers are deterministic). Randomness (jitter, trace ids) comes from one
seedable generator: ``seed(n)``.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import http.client
import inspect
import json
import logging
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import weakref
from pathlib import Path

# Names that live in a pattern module: resolved by __getattr__ below, so a pattern that isn't used costs nothing.
_PATTERN_NAMES = {
    "Policy": "policy", "Attempt": "policy",
    "retry": "retry", "aretry": "retry",
    "RetryBudget": "retry_budget",
    "retry_after_s": "retry_after",
    "attempt_timeout": "attempt_timeout",
    "CircuitBreaker": "circuit_breaker", "CircuitOpenError": "circuit_breaker",
    "Bulkhead": "bulkhead", "BulkheadFullError": "bulkhead",
    "AdaptiveThrottle": "adaptive_throttle", "ThrottledError": "adaptive_throttle",
    "AdaptiveConcurrency": "adaptive_concurrency", "ConcurrencyLimitError": "adaptive_concurrency",
    "hedge": "hedge", "ahedge": "hedge",
    "StaleCache": "fallback",
    "idempotency_key": "idempotency", "is_idempotency_conflict": "idempotency", "is_idempotency_mismatch": "idempotency",
    # not patterns, but modules that live (and are vendored) with them
    "problem": "problem", "require_approval": "problem", "set_approver": "problem", "ApprovalRequired": "problem",
    "dump_incident": "recorder",
    "AIPolicy": "ai", "TokenBudget": "ai", "ExactCache": "ai", "ToolAllowlist": "ai",
    "LoopGuard": "ai", "StepJournal": "ai", "validate_schema": "ai", "frame_untrusted": "ai",
    "check_injection_leaks": "ai", "SchemaValidationError": "ai", "ToolNotAllowedError": "ai",
    "LoopGuardError": "ai", "BudgetExceededError": "ai",
}

__all__ = [
    "RasError",
    "RejectedError",
    "DeadlineExceeded",
    "call_with_timeout",
    "deadline",
    "remaining_s",
    "is_transient",
    "dependency_failed",
    "not_accepted",
    "log_event",
    "health",
    "trace",
    "trace_id",
    "new_trace_id",
    "traceparent",
    "trace_headers",
    "fault_class",
    "seed",
    "reset_state",
    "swap",
    "TRANSIENT_HTTP",
    *_PATTERN_NAMES,
]


def __getattr__(name):
    mod = _PATTERN_NAMES.get(name)
    if mod is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    import importlib

    value = globals()[name] = getattr(importlib.import_module("ras_anything.patterns." + mod), name)
    return value


TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})

_rng = random.Random()
_trace: contextvars.ContextVar[str | None] = contextvars.ContextVar("ras_trace_id", default=None)
_span: contextvars.ContextVar[str | None] = contextvars.ContextVar("ras_span_id", default=None)
_ctx: "contextvars.ContextVar[CallContext | None]" = contextvars.ContextVar("ras_call", default=None)
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("ras_deadline", default=None)
# A call_with_timeout in progress on the virtual clock: the replayed boundary is cut at this time.
_enforced: contextvars.ContextVar[float | None] = contextvars.ContextVar("ras_enforced", default=None)
# The virtual clock, while `ras-anything verify` replays calls on one.
VIRTUAL_CLOCK = None

_stateful: "weakref.WeakSet" = weakref.WeakSet()


def track(obj):
    """Register an object whose state (tokens, open circuits, caches) reset_state() clears."""
    _stateful.add(obj)
    return obj


def reset_state() -> None:
    """Forget what every breaker, budget, throttle, bulkhead and cache has seen so far. (verify does this
    before each independent trial; tests may want it too.)"""
    for obj in list(_stateful):
        obj.reset()
    _functions.clear()
    _tripped.clear()


# --------------------------------------------------------------------------- flags (also vendored with this file)

_FLAG_PATTERN = "ras.pattern."
_FLAG_ENABLE = "ras.enable."
_FLAG_SHADOW = "ras.shadow."


def _flag_path() -> Path:
    env = os.environ.get("RAS_ANYTHING_FLAGS")
    if env:
        return Path(env)
    here = Path(os.getcwd()).resolve()
    for folder in (here, *here.parents):
        ras = folder / ".ras"
        if ras.is_dir():  # the project: do not inherit a parent repo's flags
            return ras / "flags.json"
    return here / ".ras" / "flags.json"


class _FlagStore:
    """flagd-compatible file, mtime+size poll, last-known-good, never raises to the caller."""

    def __init__(self):
        self.path = None
        self.doc = {"flags": {}}
        self.last_good = {"flags": {}}
        self.mtime_ns = 0
        self.size = -1
        self.version = 0
        self.source = "default"
        self.last_error = None
        self._loaded = False

    def bind(self, path: Path | None = None) -> "_FlagStore":
        p = Path(path) if path else _flag_path()
        if self.path is not None and self.path.resolve() != p.resolve():
            self.__init__()
        self.path = p
        return self

    def maybe_reload(self) -> None:
        if self.path is None:
            self.bind()
        try:
            st = self.path.stat()
        except FileNotFoundError:
            if self._loaded and self.source != "missing":
                self.doc = {"flags": {}}
                self.last_good = {"flags": {}}
                self.source = "missing"
                self.version += 1
            self._loaded = True
            self.mtime_ns = 0
            self.size = -1
            return
        if self._loaded and st.st_mtime_ns == self.mtime_ns and st.st_size == self.size:
            return
        try:
            doc = json.loads(self.path.read_text())
            if not isinstance(doc, dict) or not isinstance(doc.get("flags", {}), dict):
                raise ValueError("flags.json must be an object with a 'flags' object")
            doc.setdefault("flags", {})
            self.doc = self.last_good = doc
            self.last_error = None
            self.source = "file"
        except (OSError, ValueError, json.JSONDecodeError) as e:
            self.last_error = str(e)
            self.doc = self.last_good
            self.source = "last-known-good"
        self.mtime_ns = st.st_mtime_ns
        self.size = st.st_size
        self._loaded = True
        self.version += 1

    def flag(self, key: str):
        try:
            self.maybe_reload()
            f = self.doc.get("flags", {}).get(key)
            return f if isinstance(f, dict) else None
        except Exception:
            return None

    def evaluate(self, key: str, default):
        try:
            f = self.flag(key)
            if not f:
                return default
            if str(f.get("state", "ENABLED")).upper() == "DISABLED":
                if isinstance(default, bool):
                    return False
                if isinstance(default, (int, float)):
                    return 0
                return default
            variants = f.get("variants") or {}
            name = f.get("defaultVariant")
            if name in variants:
                return variants[name]
            if variants:
                return next(iter(variants.values()))
            return default
        except Exception:
            return default


_flag_store = _FlagStore()


def flag_store(path: Path | None = None) -> _FlagStore:
    return _flag_store.bind(path)


def reset_flags() -> None:
    _flag_store.__init__()


def flags_disabled() -> bool:
    return os.environ.get("RAS_ANYTHING_DISABLE") == "1"


def is_pattern_on(dependency: str, pattern: str, configured: bool) -> bool:
    if flags_disabled():
        return configured
    s = flag_store()
    specific = _FLAG_PATTERN + pattern + "@" + dependency
    if s.flag(specific):
        return bool(s.evaluate(specific, configured))
    general = _FLAG_PATTERN + pattern
    if s.flag(general):
        return bool(s.evaluate(general, configured))
    return configured


def enable_percent(fid: str) -> int:
    if flags_disabled():
        return 0
    v = flag_store().evaluate(_FLAG_ENABLE + fid, 100)
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = 100 if v else 0
    return max(0, min(100, n))


def shadow_on(fid: str) -> bool:
    if flags_disabled():
        return False
    return bool(flag_store().evaluate(_FLAG_SHADOW + fid, False))


def sticky_bucket(key: str, buckets: int = 100) -> int:
    import hashlib
    h = hashlib.sha256(str(key).encode()).digest()
    return int.from_bytes(h[:8], "big") % buckets


def in_canary(fid: str, targeting_key: str | None) -> bool:
    pct = enable_percent(fid)
    if pct >= 100:
        return True
    if pct <= 0:
        return False
    return sticky_bucket(targeting_key or fid) < pct


def targeting_key(*args, **kwargs) -> str | None:
    for k in ("idempotency_key", "targeting_key", "key", "order_id", "id"):
        if kwargs.get(k) not in (None, ""):
            return str(kwargs[k])
    if args:
        a = args[0]
        if isinstance(a, (str, int)):
            return str(a)
        if isinstance(a, dict):
            for k in ("id", "key", "order_id"):
                if a.get(k) not in (None, ""):
                    return str(a[k])
    return None


def route(fid: str, key: str | None = None) -> str:
    try:
        if flags_disabled() or enable_percent(fid) <= 0:
            return "original"
        if shadow_on(fid):
            return "shadow"
        return "hardened" if in_canary(fid, key) else "original"
    except Exception:
        return "original"


_shadow_suppressor = None


def set_shadow_suppressor(fn):
    """`fn(hardened, args, kwargs)` runs a shadow call; return the previous hook."""
    global _shadow_suppressor
    prev, _shadow_suppressor = _shadow_suppressor, fn
    return prev



def swap(fid: str, original, hardened, dependencies=None):
    """A wrapper that looks like `original` and routes each call (canary / shadow / disable).

    A bug in the router or flag evaluation itself uses the original (fail-safe). Hardened calls run
    observed(): each has a span, its failures name the dependency, and a bad one leaves an incident.
    """
    watched = observed(fid, hardened, dependencies, original)

    def pick(args, kwargs):
        try:
            return route(fid, targeting_key(*args, **kwargs) or fid)
        except Exception:
            return "original"

    def shadow(out, args, kwargs):
        try:
            if _shadow_suppressor is not None:
                other = _shadow_suppressor(hardened, args, kwargs)
                if other is not None and other != out:
                    log_event("ras.shadow.mismatch", id=fid, correlation_id=trace_id())
            else:
                log_event("ras.shadow.skip", id=fid, reason="no suppressor")
        except Exception as e:
            log_event("ras.shadow.error", id=fid, error=type(e).__name__)
        return out

    if inspect.iscoroutinefunction(original):
        async def wrapped(*args, **kwargs):
            choice = pick(args, kwargs)
            if choice == "hardened":
                return await watched(*args, **kwargs)
            out = await original(*args, **kwargs)
            return shadow(out, args, kwargs) if choice == "shadow" else out
    else:
        def wrapped(*args, **kwargs):
            choice = pick(args, kwargs)
            if choice == "hardened":
                return watched(*args, **kwargs)
            out = original(*args, **kwargs)
            return shadow(out, args, kwargs) if choice == "shadow" else out

    try:
        wrapped.__name__ = getattr(original, "__name__", "swapped")
        wrapped.__doc__ = getattr(original, "__doc__", None)
        wrapped.__wrapped__ = original
        wrapped.__ras_id__ = fid
        wrapped.__ras_hardened__ = hardened
    except Exception:
        pass
    return wrapped


def seed(n: int) -> None:
    """Make jitter and trace ids reproducible."""
    _rng.seed(n)


def _now() -> float:
    return time.monotonic()


# --------------------------------------------------------------------------- trace context (W3C)


def new_trace_id() -> str:
    return "%032x" % (_rng.getrandbits(128) or 1)


def new_span_id() -> str:
    return "%016x" % (_rng.getrandbits(64) or 1)


_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}(?:-.*)?$")


def parse_traceparent(value) -> tuple[str, str] | None:
    """(trace id, span id) of a W3C `traceparent`, or None if it isn't a valid one."""
    m = _TRACEPARENT.match(value.strip().lower()) if isinstance(value, str) else None
    if not m or value.strip()[:2].lower() == "ff" or not int(m.group(1), 16) or not int(m.group(2), 16):
        return None
    return m.group(1), m.group(2)


def trace_id() -> str:
    """The current trace id: the caller's (trace(), $TRACEPARENT), else one is created on first use in this context."""
    t = _trace.get()
    if t is None:
        outer = parse_traceparent(os.environ.get("TRACEPARENT"))
        t = outer[0] if outer else new_trace_id()
        if outer and _span.get() is None:
            _span.set(outer[1])
        _trace.set(t)
    return t


def span_id() -> str | None:
    return _span.get()


@contextlib.contextmanager
def trace(traceparent: str | None = None):
    """Everything inside belongs to one trace: the one of an incoming `traceparent` header, or a new one."""
    outer = parse_traceparent(traceparent)
    t1, t2 = _trace.set(outer[0] if outer else new_trace_id()), _span.set(outer[1] if outer else new_span_id())
    try:
        yield _trace.get()
    finally:
        _trace.reset(t1)
        _span.reset(t2)


def traceparent() -> str:
    """The W3C `traceparent` to send along with a dependency call, so its logs and ours share a trace."""
    return "00-%s-%s-01" % (trace_id(), _span.get() or new_span_id())


def trace_headers() -> dict:
    return {"traceparent": traceparent()}


# --------------------------------------------------------------------------- masking

_MASKS = [(re.compile(p, f), r) for p, f, r in (
    (r"(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}", re.I, "[token]"),
    (r"\beyJ[\w-]{8,}\.[\w-]+\.[\w-]*", 0, "[token]"),
    (r"\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|xox[abprs]|AKIA|ASIA|AIza)[-_A-Za-z0-9]{12,}", 0, "[token]"),
    (r"\b((?:api[_-]?key|access[_-]?key|token|secret|passw(?:or)?d|pwd|authorization|session|signature|sig)[\"']?\s*[=:]\s*[\"']?)[^\s\"'&,;}]+", re.I, r"\1[secret]"),
    (r"(?<=://)[^/\s:@]+:[^/\s@]+@", 0, "[secret]@"),
    (r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", 0, "[email]"),
)]
_CARD = re.compile(r"(?<![\w.-])\d(?:[ -]?\d){12,18}(?![\w-]|\.\d)")
_PHONE = re.compile(r"(?<![\w.:/-])(?<!\d[ .-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?|\d{2,4}[ .-])\d{3,4}[ .-]\d{3,4}(?![\w-]|\.\d|[ .-]\d)|(?<![\w.:/-])\+\d{8,14}(?!\w)")


def _luhn(text: str) -> bool:
    d = [int(c) for c in text if c.isdigit()][::-1]
    return sum(x if i % 2 == 0 else (2 * x - 9 if x > 4 else 2 * x) for i, x in enumerate(d)) % 10 == 0


def mask(text: str) -> str:
    """Text without e-mail addresses, phone and card numbers, tokens and secrets."""
    for rx, repl in _MASKS:
        text = rx.sub(repl, text)
    text = _CARD.sub(lambda m: "[card]" if _luhn(m.group()) else m.group(), text)
    return _PHONE.sub("[phone]", text)


_INTERNALS = [(re.compile(p, f), r) for p, f, r in (
    (r"Traceback \(most recent call last\):.*", re.S, "[trace]"),
    (r"\bFile \"[^\"]+\", line \d+(?:, in \S+)?", 0, "[path]"),
    (r"\n\s+at .*", re.S, ""),
    (r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>)\]]+", re.I, "[url]"),
    (r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?![\w.])", 0, "[host]"),
    (r"\[?(?<![\w:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{1,4}\]?(?::\d+)?(?![\w:])", re.I, "[host]"),
    (r"\blocalhost(?::\d+)?\b", 0, "[host]"),
    (r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:com|net|org|io|dev|app|local|internal|lan|corp|example|test|cloud|ai|co|svc|cluster)\b(?::\d+)?", re.I, "[host]"),
    (r"(?<![\w.])(?:~|\.{1,2})?/(?:[\w.@+-]+/)+[\w.@+-]*", 0, "[path]"),
    (r"\b[A-Za-z]:\\\\?[^\s\"']+", 0, "[path]"),
    (r"(?<![\w./])[\w-]+\.(?:py|mjs|cjs|js|ts|go|rb|java|php|cs)(?::\d+)?\b", 0, "[path]"),
)]


def plain(text: str, limit: int = 200) -> str:
    """mask(), and without the inside of this system either: hosts, URLs, paths, stack frames. For people and agents
    outside; logs keep them."""
    text = mask(str(text))
    for rx, repl in _INTERNALS:
        text = rx.sub(repl, text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- errors


class RasError(Exception):
    """A structured error. str() is one short sentence anyone may see; .problem / to_json() is the same for an
    agent (RFC 9457 problem+json: what failed, is a retry worth it, what to do next); `context` and the cause are
    for our own logs (to_dict()).

    Cause, dependency and advice are worked out from `cause=` and the call in progress; keywords override them:
    dependency, retryable, retry_after_s, next_action, needs_human_authorization, category, status."""

    def __init__(self, message: str, *, cause: BaseException | None = None, context: dict | None = None,
                 trace_id: str | None = None, **advice):
        super().__init__(message)
        self.message = message
        self.cause = cause
        self.context = dict(context or {})
        self.trace_id = trace_id or globals()["trace_id"]()
        self.advice = advice
        if cause is not None:
            self.__cause__ = cause
        ctx = _ctx.get()
        if ctx is not None and "dependency" not in advice:
            hit = ctx.last_dependency or (ctx.deps[0]["name"] if len(ctx.deps) == 1 else None)
            if hit:
                self.advice = {**advice, "dependency": hit}

    @property
    def problem(self) -> dict:
        try:
            return _lazy("problem", "problem")(self)
        except Exception:  # (the module isn't there)
            return {"type": "about:blank", "title": "Error", "status": 500, "detail": plain(self.message), "instance": self.trace_id,
                    "correlation_id": self.trace_id}

    def to_json(self) -> str:
        return json.dumps(self.problem, ensure_ascii=False)

    def __str__(self) -> str:
        p = self.problem
        why = "; ".join(x for x in ("cause: %s" % p["cause"] if p.get("cause") not in (None, "unknown") else "",
                                    "next: %s" % p["next_action"] if p.get("next_action") else "") if x)
        return "%s (%strace_id=%s)" % (plain(self.message), why + "; " if why else "", self.trace_id)

    def to_dict(self) -> dict:
        return {
            "error": type(self).__name__,
            "message": self.message,
            "cause": None if self.cause is None else "%s: %s" % (type(self.cause).__name__, self.cause),
            "context": self.context,
            "trace_id": self.trace_id,
        }

    def __reduce__(self):
        return (_rebuild_ras_error, (type(self), self.message, self.context, self.trace_id, self.advice))


def _rebuild_ras_error(cls, message, context, trace_id, advice=None):
    e = cls(message, context=context, trace_id=trace_id)
    e.advice = dict(advice or {})
    return e


def _lazy(module: str, name: str):
    import importlib

    return getattr(importlib.import_module("ras_anything.patterns." + module), name)


class RejectedError(RasError):
    """A pattern of this runtime refused to call the dependency (open circuit, full bulkhead, load shed, ...)."""


class DeadlineExceeded(TimeoutError):
    """The caller's deadline has passed: there is no time left for (another) attempt."""


# --------------------------------------------------------------------------- calls in progress


class CallContext:
    """One call of a hardened function: its span, what it may call, and what happened in it (for the flight recorder)."""

    __slots__ = ("fid", "deps", "trace_id", "span_id", "parent", "seed", "_rng", "events", "dropped", "t0", "ts", "args", "kwargs", "fn", "gave_up",
                 "last_dependency", "health")
    MAX_EVENTS = 200

    def __init__(self, fid, deps, trace_, span, parent, fn=None, args=(), kwargs=None):
        self.fid, self.deps, self.trace_id, self.span_id, self.parent = fid, deps, trace_, span, parent
        self.seed = _rng.getrandbits(48)
        self._rng = None
        self.events: list = []
        self.dropped = 0
        self.t0, self.ts = _now(), time.time()
        self.fn, self.args, self.kwargs = fn, args, kwargs or {}
        self.gave_up = None          # the fault class of the last dependency failure nothing could be done about
        self.last_dependency = None
        self.health = None

    @property
    def rng(self):
        if self._rng is None:
            self._rng = random.Random(self.seed)
        return self._rng

    def note(self, event: str, **facts) -> None:
        if len(self.events) < self.MAX_EVENTS:
            self.events.append({"t": round(_now() - self.t0, 3), "event": event, **{k: v for k, v in facts.items() if v is not None}})
        else:
            self.dropped += 1


def rng() -> random.Random:
    """Randomness for jitter: the call's own generator (so an incident can be replayed), else the process's."""
    ctx = _ctx.get()
    return _rng if ctx is None else ctx.rng


_functions: dict = {}  # id -> counters, for health()
_tripped: dict = {}    # (kind, name) -> what isn't at rest (open breakers)


def _enter(fid, deps, fn, args, kwargs):
    t = trace_id() if _ctx.get() is not None or _trace.get() is not None or os.environ.get("TRACEPARENT") else new_trace_id()
    ctx = CallContext(fid, deps, t, new_span_id(), _span.get(), fn, args, kwargs)
    ctx.health = list(_tripped.values()) if _tripped else None
    return ctx, (_trace.set(t), _span.set(ctx.span_id), _ctx.set(ctx))


def _leave(entered, outcome, error=None, result=None):
    ctx, tokens = entered
    for var, tok in zip((_trace, _span, _ctx), tokens):
        var.reset(tok)
    try:
        st = _functions.setdefault(ctx.fid, {"calls": 0, "raised": 0, "gave_up": 0, "last_fault": None, "last_incident": None})
        st["calls"] += 1
        st["raised"] += outcome == "raise"
        st["gave_up"] += ctx.gave_up is not None
        if ctx.gave_up is not None or outcome == "raise":
            st["last_fault"] = ctx.gave_up or fault_class(error)
        iid = _lazy("recorder", "finish")(ctx, outcome, error, result)
        if iid:
            st["last_incident"] = iid
    except Exception:  # recording must never break the caller
        pass
    _write_health()


def observed(fid: str, fn, dependencies=None, original=None):
    """fn, each call with a span of its own; `dependencies` (what recording saw it call) lets diagnostics name the
    dependency that failed. Used by swap()."""
    deps = list(dependencies or ())
    like = original if original is not None else fn

    def enter(args, kwargs):
        try:
            return _enter(fid, deps, like, args, kwargs)
        except Exception:
            return None

    if inspect.iscoroutinefunction(fn):
        async def call(*args, **kwargs):
            entered = enter(args, kwargs)
            if entered is None:
                return await fn(*args, **kwargs)
            try:
                result = await fn(*args, **kwargs)
            except BaseException as e:
                _leave(entered, "raise", e)
                raise
            _leave(entered, "return", None, result)
            return result
    else:
        def call(*args, **kwargs):
            entered = enter(args, kwargs)
            if entered is None:
                return fn(*args, **kwargs)
            try:
                result = fn(*args, **kwargs)
            except BaseException as e:
                _leave(entered, "raise", e)
                raise
            _leave(entered, "return", None, result)
            return result
    return functools.wraps(fn)(call)


# --------------------------------------------------------------------------- diagnostics (OpenTelemetry log data model)

_SEVERITY = {"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17}
_LEVEL = {"off": 99, "error": 17, "warn": 13, "info": 9, "debug": 5}
SLOW_S = 1.0  # an answered attempt that took longer than this is reported ($RAS_ANYTHING_SLOW_S)


class _Stderr(logging.Handler):
    """Writes to whatever sys.stderr is now."""

    def emit(self, record):
        try:
            sys.stderr.write(record.getMessage() + "\n")
        except Exception:  # logging must never break the caller
            pass


logger = logging.getLogger("ras_anything")
if not logger.handlers:
    logger.addHandler(_Stderr())
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # (add your own handler, or set propagate, to send records elsewhere)


def _value(v):
    if isinstance(v, (int, float, bool, type(None))):
        return v
    return mask(v if isinstance(v, str) else repr(v))


_SERVICE = os.environ.get("OTEL_SERVICE_NAME") or os.path.basename(sys.argv[0] if getattr(sys, "argv", None) else "") or "python"


def emit(event: str, severity: str = "INFO", body: str | None = None, attributes: dict | None = None, *, legacy: bool = False,
         trace: str | None = None) -> None:
    """One log record in the OpenTelemetry log data model, as a JSON line through the `ras_anything` logger."""
    try:
        if _SEVERITY[severity] < _LEVEL.get(os.environ.get("RAS_ANYTHING_LOG", "info").lower(), 9) and not legacy:
            return
        now, ctx = time.time(), _ctx.get()
        tid = trace or (ctx.trace_id if ctx is not None else trace_id())
        rec = {"ts": round(now, 3), "event": event, "trace_id": tid} if legacy else {}  # (the keys log_event has always had)
        rec.update({"Timestamp": str(int(now * 1e9)), "SeverityText": severity, "SeverityNumber": _SEVERITY[severity], "EventName": event,
                    "TraceId": tid, "SpanId": _span.get(), "Body": mask(body or event), "Resource": {"service.name": _SERVICE}})
        attrs = {k: _value(v) for k, v in (attributes or {}).items() if v is not None or legacy}
        if ctx is not None:
            attrs.setdefault("code.function", ctx.fid)
        rec["Attributes"] = attrs
        if rec["SpanId"] is None:
            del rec["SpanId"]
        logger.log({"ERROR": 40, "WARN": 30, "INFO": 20}.get(severity, 10), "%s", json.dumps(rec))
    except Exception:  # logging must never break the caller
        pass


def log_event(event: str, **fields) -> None:
    """One JSON line on stderr for something this code decided (a give-up, a fallback): `event`, `trace_id`, and the
    fields as attributes, plus the OpenTelemetry log record around them."""
    tid = fields.pop("trace_id", None)
    ctx, err = _ctx.get(), fields.get("error")
    attrs = dict(fields)
    severity = "WARN" if not event.startswith(("ras.approval.granted", "ras.shadow.skip", "ras.canary.promote")) else "INFO"
    try:
        if isinstance(err, BaseException):
            attrs["error"] = "%s: %s" % (type(err).__name__, err)
            attrs.update(_error_attributes(err, dependency=fields.get("dependency")))
        elif ctx is not None and ctx.last_dependency:
            attrs.setdefault("ras.dependency", ctx.last_dependency)
        if ctx is not None:
            ctx.note("log", name=event)
    except Exception:
        pass
    emit(event, severity, None, attrs, legacy=True, trace=tid)


# What a failure was, in the words of the fault model.
def fault_class(exc: BaseException | None) -> str:
    """timeout, refused, unavailable, rate_limited, truncated, invalid_request, auth, conflict, quota, circuit_open,
    rejected, approval, unsupported, bug or unknown."""
    seen = 0
    e = exc
    while e is not None and seen < 6:
        c = _class_of(e)
        if c is not None:
            return c
        nxt = getattr(e, "cause", None)
        e, seen = nxt if isinstance(nxt, BaseException) else e.__cause__ or (e.__context__ if not e.__suppress_context__ else None), seen + 1
    if isinstance(exc, (TypeError, KeyError, AttributeError, IndexError, NameError, ZeroDivisionError, AssertionError, RecursionError)):
        return "bug"
    return "unknown"


def status_class(status: int) -> str | None:
    if status == 429:
        return "rate_limited"
    if status in (401, 403, 407):
        return "auth"
    if status == 402:
        return "quota"
    if status in (408, 504):
        return "timeout"
    if status in (409, 412, 423):
        return "conflict"
    if status in (501, 505):
        return "unsupported"
    if 400 <= status < 500:
        return "unavailable" if status == 425 else "invalid_request"
    return "unavailable" if status >= 500 else None


def _class_of(e: BaseException) -> str | None:
    name = type(e).__name__
    if isinstance(e, RejectedError):
        return {"CircuitOpenError": "circuit_open"}.get(name, "rejected")
    if getattr(e, "needs_human_authorization", False):
        return "approval"
    status = _http_status(e)
    if status is not None:
        c = status_class(status)
        return "quota" if c == "rate_limited" and "quota" in str(getattr(e, "body", "") or e).lower() else c
    text = str(e).lower()
    if isinstance(e, (TimeoutError, socket.timeout, subprocess.TimeoutExpired)) or "timeout" in name.lower():
        return "timeout"
    if isinstance(e, urllib.error.URLError) and isinstance(e.reason, BaseException):
        return _class_of(e.reason) or "unavailable"
    if isinstance(e, (ConnectionRefusedError, BlockingIOError, socket.gaierror)) or "refused" in text:
        return "refused"
    if isinstance(e, (http.client.IncompleteRead, json.JSONDecodeError)) or name in ("ChunkedEncodingError", "ClientPayloadError", "RemoteProtocolError"):
        return "truncated"
    if isinstance(e, ConnectionError) or name in ("ConnectError", "NetworkError", "ClientConnectionError", "ServerDisconnectedError"):
        return "unavailable"
    if isinstance(e, subprocess.CalledProcessError):
        return "unavailable" if e.returncode == 75 or e.returncode < 0 else None
    if isinstance(e, RasError) or type(e) in (KeyError, AttributeError, TypeError, NameError, IndexError):
        return None
    for words, c in ((("timed out", "timeout", "deadline"), "timeout"), (("exited 75", "exit 75", "tempfail", "temporarily unavailable", " 503"), "unavailable"),
                     (("429", "rate limit", "too many requests"), "rate_limited"), (("truncated", "incomplete", "unterminated"), "truncated")):
        if any(w in text for w in words):
            return c
    return None


def _target_of(e: BaseException) -> dict:
    """What an error says about where it came from: {"url"} or {"argv"}; also from the boundary call's own frame in
    its traceback (urllib's errors don't name the URL)."""
    seen = 0
    while e is not None and seen < 6:
        url = getattr(e, "url", None) or getattr(getattr(e, "request", None), "url", None)
        if isinstance(url, str) and "://" in url or url is not None and type(url).__name__ == "URL":
            return {"url": str(url)}
        argv = getattr(e, "cmd", None)
        if argv:
            return {"argv": argv if isinstance(argv, (list, tuple)) else str(argv).split()}
        tb, hit = e.__traceback__, {}
        while tb is not None:
            code, loc = tb.tb_frame.f_code, tb.tb_frame.f_locals
            if code.co_name in ("urlopen", "_urlopen", "request", "send"):
                u = loc.get("url") or loc.get("request")
                u = getattr(u, "full_url", None) or getattr(u, "url", None) or u
                if u is not None and "://" in str(u):
                    hit = {"url": str(u)}
            elif code.co_name in ("run", "_run", "check_output"):
                a = loc.get("popenargs") or loc.get("args")
                a = a[0] if isinstance(a, tuple) and a and isinstance(a[0], (list, tuple)) else a
                if isinstance(a, (list, tuple)) and a:
                    hit = {"argv": [str(x) for x in a]}
            tb = tb.tb_next
        if hit:
            return hit
        e, seen = getattr(e, "cause", None) if isinstance(getattr(e, "cause", None), BaseException) else e.__cause__, seen + 1
    return {}


def _where(exc, dependency=None) -> dict:
    """The dependency a failure (or a slow answer) belongs to, as attributes."""
    ctx = _ctx.get()
    deps = ctx.deps if ctx is not None else []
    target = _target_of(exc) if exc is not None else {}
    out: dict = {}
    hit = deps[0] if len(deps) == 1 else None
    if target.get("url"):
        u = urllib.parse.urlsplit(target["url"])
        out.update({"server.address": u.hostname, "server.port": u.port, "url.path": u.path})
        hit = next((d for d in deps if d.get("route") and (u.path or "/").startswith(d["route"])), hit)
    elif target.get("argv"):
        words = [os.path.basename(str(x)) for x in target["argv"][:4]]
        out["process.command"] = " ".join(words)
        hit = next((d for d in deps if d.get("program") and d["program"] in words), hit)
    if hit:
        out.setdefault("url.template", hit.get("route"))
        out.setdefault("process.command", hit.get("program"))
        out["http.request.method"] = hit.get("method")
    name = dependency or (hit or {}).get("name")
    if name:
        out["ras.dependency"] = name
        if ctx is not None:
            ctx.last_dependency = (hit or {}).get("name") or name
    return {k: v for k, v in out.items() if v is not None}


def _error_attributes(exc, dependency=None) -> dict:
    status = _http_status(exc)
    name = type(exc).__name__ if not isinstance(exc, RasError) or exc.cause is None else type(exc.cause).__name__
    return {"error.type": str(status) if status else name, "http.response.status_code": status,
            "ras.fault": fault_class(exc), "exception.message": plain(str(exc), 200), **_where(exc, dependency)}


def _advice(exc) -> dict:
    try:
        p = _lazy("problem", "problem")(exc)
        return {"ras.cause": p["cause"], "ras.retryable": p["retryable"], "ras.next_action": p["next_action"],
                "ras.retry_after_s": p.get("retry_after_s")}
    except Exception:
        return {}


def attempt_failed(exc, n: int, took_s: float, *, dependency=None, key=None) -> None:
    """One attempt of a dependency call failed (called by retry and Policy)."""
    try:
        if getattr(exc, "_ras_reported", None) == n or isinstance(exc, RejectedError):
            return
        attrs = _error_attributes(exc, dependency)
        ctx = _ctx.get()
        if ctx is not None:
            ctx.note("failure", n=n, dependency=attrs.get("ras.dependency"), fault=attrs["ras.fault"], took_s=round(took_s, 3), error=attrs["error.type"],
                     retry_after_s=_retry_after(exc))
        emit("ras.dependency.failure", "WARN", "%s failed: %s (attempt %d)" % (attrs.get("ras.dependency") or "a dependency", attrs["ras.fault"], n),
             {**attrs, "http.request.resend_count": n - 1, "ras.duration_s": round(took_s, 3), "ras.idempotency_key": key})
        try:
            exc._ras_reported = n
        except Exception:
            pass
    except Exception:
        pass


def _retry_after(exc):
    try:
        return _lazy("retry_after", "retry_after_s")(exc)
    except Exception:
        return None


def retrying(exc, n: int, delay_s: float, *, dependency=None) -> None:
    """Attempt n failed and another follows after delay_s."""
    try:
        ctx = _ctx.get()
        if ctx is not None:
            ctx.note("retry", n=n, delay_s=round(delay_s, 3))
        emit("ras.retry", "INFO", "retry %d in %.3gs" % (n, delay_s), {"http.request.resend_count": n, "ras.retry.delay_s": round(delay_s, 3),
                                                                       "ras.dependency": dependency or (ctx and ctx.last_dependency)})
    except Exception:
        pass


def gave_up(exc, n: int, why: str, *, dependency=None, key=None) -> None:
    """No further attempt: the caller gets the error (or degrades)."""
    try:
        if getattr(exc, "_ras_gave_up", False):
            return
        attrs = _error_attributes(exc, dependency)
        ctx = _ctx.get()
        if ctx is not None:
            ctx.gave_up = attrs["ras.fault"]
            ctx.note("gave_up", n=n, why=why, fault=attrs["ras.fault"], dependency=attrs.get("ras.dependency"))
        emit("ras.dependency.gave_up", "ERROR", "%s: gave up after %d attempt(s): %s (%s)" % (
            attrs.get("ras.dependency") or "a dependency", n, attrs["ras.fault"], why),
            {**attrs, "http.request.resend_count": max(0, n - 1), "ras.give_up": why, "ras.idempotency_key": key, **_advice(exc)})
        try:
            exc._ras_gave_up = True
        except Exception:
            pass
    except Exception:
        pass


def attempt_ok(n: int, took_s: float, *, dependency=None) -> None:
    """An attempt was answered: worth a record if it was slow, or the one that worked after failures."""
    try:
        limit = float(os.environ.get("RAS_ANYTHING_SLOW_S") or SLOW_S)
        if took_s < limit and n <= 1:
            return
        ctx = _ctx.get()
        if took_s >= limit:
            attrs = _where(None, dependency)
            if ctx is not None:
                ctx.note("slow", n=n, took_s=round(took_s, 3), dependency=attrs.get("ras.dependency"))
            emit("ras.dependency.slow", "WARN", "%s answered after %.3gs" % (attrs.get("ras.dependency") or "a dependency", took_s),
                 {**attrs, "ras.fault": "slow", "ras.duration_s": round(took_s, 3), "http.request.resend_count": n - 1})
        if n > 1:
            if ctx is not None:
                ctx.note("recovered", n=n)
            emit("ras.dependency.recovered", "INFO", "answered on attempt %d" % n, {**_where(None, dependency), "http.request.resend_count": n - 1})
    except Exception:
        pass


def transition(kind: str, name: str, before: str, after: str, **facts) -> None:
    """A breaker (or another guard) changed state."""
    try:
        if after == "closed":
            _tripped.pop((kind, name), None)
        else:
            _tripped[kind, name] = {"kind": kind, "name": name, "state": after, "since": round(_now(), 3)}
        ctx = _ctx.get()
        if ctx is not None:
            ctx.note(kind, name=name, before=before, after=after)
        emit("ras.%s.transition" % kind, "INFO" if after == "closed" else "WARN", "%s %s: %s -> %s" % (kind, name, before, after),
             {"ras.dependency": name, "ras.%s.state" % kind: after, "ras.%s.previous_state" % kind: before, **facts})
        _write_health(force=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- health


def _states() -> list:
    """The state of every breaker, budget, ... that has one to tell and is not at rest."""
    out = []
    for obj in list(_stateful):
        try:
            h = obj.health()
        except AttributeError:
            continue
        except Exception:
            continue
        if h:
            out.append(h)
    return sorted(out, key=lambda h: (h.get("kind", ""), h.get("name", "")))


def health() -> dict:
    """For a health endpoint: {"live", "ready", "status": "ok" | "degraded", "breakers": [...], "functions": {...}}.
    Degraded (and not ready) while a breaker is open. `live` only says this code runs."""
    states = _states()
    breakers = [s for s in states if s.get("kind") == "breaker"]
    down = [b["name"] for b in breakers if b["state"] == "open"]
    return {"schema": "ras-anything.health/1", "time": round(time.time(), 3), "service": _SERVICE, "pid": os.getpid(), "live": True, "ready": not down,
            "status": "degraded" if down else "ok", "disabled": flags_disabled(), "breakers": breakers,
            "guards": [s for s in states if s.get("kind") != "breaker"],
            "functions": {k: dict(v) for k, v in _functions.items()}}


_health_written = [0.0]


def _write_health(force: bool = False) -> None:
    """health() as a file, when $RAS_ANYTHING_HEALTH_FILE is set: on every transition, else once a second at most."""
    path = os.environ.get("RAS_ANYTHING_HEALTH_FILE")
    if not path or (not force and abs(_now() - _health_written[0]) < 1.0):
        return
    _health_written[0] = _now()
    try:
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w") as f:
            json.dump(health(), f)
        os.replace(tmp, path)
    except Exception:
        pass


# --------------------------------------------------------------------------- timeouts


def remaining_s() -> float | None:
    """Seconds left before the innermost deadline around this code (deadline(), a Policy's deadline,
    call_with_timeout), or None."""
    ends = [d for d in (_deadline.get(), _enforced.get()) if d is not None]
    return max(0.0, min(ends) - _now()) if ends else None


@contextlib.contextmanager
def deadline(timeout_s: float):
    """Everything inside shares one deadline, timeout_s from now; a nested deadline can only be earlier.

    It travels with the context (threads started with a copied context, asyncio tasks). It can't interrupt
    a blocking call by itself: give the call ``timeout=attempt_timeout(3.0)``, which is cut to what is left.
    retry() and Policy honour it: no attempt and no wait crosses it."""
    token = set_deadline(_now() + timeout_s)
    try:
        yield
    finally:
        _deadline.reset(token)


def set_deadline(at: float | None):
    """Make `at` (monotonic seconds) the deadline of this context unless an earlier one is set; returns the reset token."""
    cur = _deadline.get()
    return _deadline.set(at if cur is None else cur if at is None else min(cur, at))


def clear_deadline(token) -> None:
    """Undo a set_deadline."""
    _deadline.reset(token)


def enforced_remaining_s() -> float | None:
    """Seconds left of a call_with_timeout running on the virtual clock: the only limit that cuts a call
    which wasn't given a timeout itself."""
    d = _enforced.get()
    return None if d is None else max(0.0, d - _now())


def call_with_timeout(fn, timeout_s: float, *args, **kwargs):
    """Run fn(*args, **kwargs); raise TimeoutError if it takes longer than timeout_s.

    The call runs in a daemon thread (Python can't kill it: prefer passing ``timeout=`` to the
    boundary call itself; use this for code that offers no timeout). Under ``ras-anything verify``
    it runs inline against a virtual deadline.
    """
    if VIRTUAL_CLOCK:
        cur, end = _enforced.get(), _now() + timeout_s
        token = _enforced.set(end if cur is None else min(cur, end))
        try:
            return fn(*args, **kwargs)
        finally:
            _enforced.reset(token)
    box: dict = {}
    ctx = contextvars.copy_context()
    ctx.run(set_deadline, _now() + timeout_s)

    def target():
        try:
            box["value"] = ctx.run(fn, *args, **kwargs)
        except BaseException as e:  # handed to the caller below
            box["error"] = e

    t = threading.Thread(target=target, name="ras-call-with-timeout", daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError("call did not finish within %.3gs" % timeout_s)
    if "error" in box:
        raise box["error"]
    return box.get("value")


# --------------------------------------------------------------------------- what a failure means


def _http_status(exc: BaseException) -> int | None:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code
    resp = getattr(exc, "response", None)  # requests.HTTPError, httpx.HTTPStatusError
    code = getattr(resp, "status_code", None)
    if code is None and (type(exc).__module__ or "").startswith("aiohttp"):  # ClientResponseError
        code = getattr(exc, "status", None)
    return code if isinstance(code, int) and code else None


def is_transient(exc: BaseException) -> bool:
    """Timeouts, connection errors, EAGAIN, truncated responses, 408/425/429/500/502/503/504. Never other 4xx."""
    status = _http_status(exc)
    if status is not None:
        return status in TRANSIENT_HTTP
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, BlockingIOError, subprocess.TimeoutExpired,
                        http.client.IncompleteRead, http.client.RemoteDisconnected)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (OSError, socket.timeout)) or "timed out" in str(exc.reason)
    mod = type(exc).__module__ or ""
    if mod.startswith("asyncio") and type(exc).__name__ == "TimeoutError":  # its own class before Python 3.11
        return True
    if mod.startswith("requests"):
        name = type(exc).__name__
        return name in {"ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout", "ChunkedEncodingError"}
    if mod.startswith("httpx"):  # connect/read/write/pool timeouts and network errors
        return any(c.__name__ in {"TimeoutException", "NetworkError", "RemoteProtocolError"} for c in type(exc).__mro__)
    if mod.startswith("aiohttp"):
        return any(c.__name__ in {"ClientConnectionError", "ClientPayloadError"} for c in type(exc).__mro__)
    return False


def not_accepted(exc: BaseException) -> bool:
    """Did the dependency not take this request? (what throttles and concurrency limits count: transient errors, but
    not our own deadline running out before the dependency was asked)"""
    return is_transient(exc) and not isinstance(exc, DeadlineExceeded)


def dependency_failed(exc: BaseException) -> bool:
    """Is the dependency in trouble? (what circuit breakers count: not_accepted, except a 429: it is up, and asks us to
    come back later)"""
    return not_accepted(exc) and _http_status(exc) != 429


def note(e: BaseException, text: str) -> None:
    if hasattr(e, "add_note"):
        try:
            e.add_note(text)
        except Exception:
            pass
