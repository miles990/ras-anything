"""Reliability helpers for hardened code. Standard library only.

Hardened functions may import these:

    from ras_anything.runtime import Policy, retry, call_with_timeout, CircuitBreaker, RasError, log_event

This module is the core (clock, randomness, trace ids, errors, logging, deadlines, what counts as
transient). Each resilience pattern is a module of its own in ras_anything/patterns/ and is loaded
when a name of it is first used here (docs/PATTERNS.md).

Everything that sleeps or reads the clock looks up ``time.sleep`` / ``time.monotonic`` at call
time, so ``ras-anything verify`` can run your code on a virtual clock (hangs and backoff cost no
wall time and latency numbers are deterministic). Randomness (jitter, trace ids) comes from one
seedable generator: ``seed(n)``.
"""

from __future__ import annotations

import contextlib
import contextvars
import http.client
import json
import random
import socket
import subprocess
import sys
import threading
import time
import urllib.error
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
    "trace_id",
    "new_trace_id",
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


# --------------------------------------------------------------------------- flags (also vendored with this file)

_FLAG_PATTERN = "ras.pattern."
_FLAG_ENABLE = "ras.enable."
_FLAG_SHADOW = "ras.shadow."


def _flag_path() -> Path:
    import os
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
    import os
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



def swap(fid: str, original, hardened):
    """A wrapper that looks like `original` and routes each call (canary / shadow / disable).

    A bug in the router or flag evaluation itself uses the original (fail-safe).
    """

    def wrapped(*args, **kwargs):
        try:
            choice = route(fid, targeting_key(*args, **kwargs) or fid)
        except Exception:
            choice = "original"
        if choice == "original":
            return original(*args, **kwargs)
        if choice == "shadow":
            out = original(*args, **kwargs)
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
        return hardened(*args, **kwargs)

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


# --------------------------------------------------------------------------- errors


def new_trace_id() -> str:
    return "%016x" % _rng.getrandbits(64)


def trace_id() -> str:
    """The current trace id (one is created on first use in this context)."""
    t = _trace.get()
    if t is None:
        t = new_trace_id()
        _trace.set(t)
    return t


class RasError(Exception):
    """A structured error: what failed, why (cause), where (context) and a trace id to grep for."""

    def __init__(self, message: str, *, cause: BaseException | None = None, context: dict | None = None,
                 trace_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.cause = cause
        self.context = dict(context or {})
        self.trace_id = trace_id or globals()["trace_id"]()
        if cause is not None:
            self.__cause__ = cause

    def __str__(self) -> str:
        parts = [self.message]
        if self.context:
            parts.append(" ".join("%s=%s" % (k, v) for k, v in self.context.items()))
        if self.cause is not None:
            parts.append("cause=%s: %s" % (type(self.cause).__name__, self.cause))
        parts.append("trace_id=%s" % self.trace_id)
        return " | ".join(parts)

    def to_dict(self) -> dict:
        return {
            "error": type(self).__name__,
            "message": self.message,
            "cause": None if self.cause is None else "%s: %s" % (type(self.cause).__name__, self.cause),
            "context": self.context,
            "trace_id": self.trace_id,
        }

    def __reduce__(self):
        return (_rebuild_ras_error, (type(self), self.message, self.context, self.trace_id))


def _rebuild_ras_error(cls, message, context, trace_id):
    return cls(message, context=context, trace_id=trace_id)


class RejectedError(RasError):
    """A pattern of this runtime refused to call the dependency (open circuit, full bulkhead, load shed, ...)."""


class DeadlineExceeded(TimeoutError):
    """The caller's deadline has passed: there is no time left for (another) attempt."""


def log_event(event: str, **fields) -> None:
    """One JSON line on stderr: {"ts", "event", "trace_id", ...fields}."""
    rec = {"ts": round(time.time(), 3), "event": event, "trace_id": fields.pop("trace_id", None) or trace_id()}
    for k, v in fields.items():
        rec[k] = v if isinstance(v, (str, int, float, bool, type(None))) else repr(v)
    try:
        sys.stderr.write(json.dumps(rec, sort_keys=False) + "\n")
    except Exception:  # logging must never break the caller
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
