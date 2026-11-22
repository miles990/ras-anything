"""Reliability helpers for hardened code. Standard library only.

Hardened functions may import these:

    from ras_anything.runtime import retry, call_with_timeout, CircuitBreaker, RasError, log_event

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

__all__ = [
    "RasError",
    "CircuitOpenError",
    "CircuitBreaker",
    "call_with_timeout",
    "retry",
    "is_transient",
    "retry_after_s",
    "log_event",
    "trace_id",
    "new_trace_id",
    "seed",
    "TRANSIENT_HTTP",
]

TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})

_rng = random.Random()
_trace: contextvars.ContextVar[str | None] = contextvars.ContextVar("ras_trace_id", default=None)
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("ras_deadline", default=None)
# Set by `ras-anything verify` while it replays calls on a virtual clock.
VIRTUAL_CLOCK = False


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


class CircuitOpenError(RasError):
    """Raised without calling the dependency while the breaker is open."""


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
    """Seconds left before the innermost call_with_timeout deadline, or None."""
    d = _deadline.get()
    return None if d is None else max(0.0, d - _now())


@contextlib.contextmanager
def deadline(timeout_s: float):
    """Bound everything inside to timeout_s (nested deadlines keep the earliest)."""
    d = _now() + timeout_s
    cur = _deadline.get()
    token = _deadline.set(d if cur is None else min(cur, d))
    try:
        yield
    finally:
        _deadline.reset(token)


def call_with_timeout(fn, timeout_s: float, *args, **kwargs):
    """Run fn(*args, **kwargs); raise TimeoutError if it takes longer than timeout_s.

    The call runs in a daemon thread (Python can't kill it: prefer passing ``timeout=`` to the
    boundary call itself; use this for code that offers no timeout). Under ``ras-anything verify``
    it runs inline against a virtual deadline.
    """
    if VIRTUAL_CLOCK:
        with deadline(timeout_s):
            return fn(*args, **kwargs)
    box: dict = {}
    ctx = contextvars.copy_context()

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


# --------------------------------------------------------------------------- retry


def _http_status(exc: BaseException) -> int | None:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code
    resp = getattr(exc, "response", None)  # requests.HTTPError
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def is_transient(exc: BaseException) -> bool:
    """Timeouts, connection errors, truncated responses, 408/425/429/5xx(500, 502-504). Never other 4xx."""
    status = _http_status(exc)
    if status is not None:
        return status in TRANSIENT_HTTP
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, subprocess.TimeoutExpired,
                        http.client.IncompleteRead, http.client.RemoteDisconnected)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (OSError, socket.timeout)) or "timed out" in str(exc.reason)
    mod = type(exc).__module__ or ""
    if mod.startswith("requests"):
        name = type(exc).__name__
        return name in {"ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout", "ChunkedEncodingError"}
    return False


def retry_after_s(exc: BaseException) -> float | None:
    """The Retry-After delay (seconds) an HTTP error asked for, if any."""
    headers = getattr(exc, "headers", None)
    if headers is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except Exception:
        return None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except Exception:
            return None


def retry(fn, *, attempts: int = 3, base: float = 0.1, cap: float = 2.0, budget_s: float | None = None,
          retry_on=is_transient, sleep=None, on_retry=None):
    """Call fn() up to `attempts` times, retrying only errors `retry_on` accepts.

    Backoff is "full jitter" (AWS): sleep = random(0, min(cap, base * 2**n)), at least the
    server's Retry-After when it sent one. `budget_s` bounds the total time: a retry whose wait
    would cross it is not attempted. The last error is re-raised unchanged (same type), with a
    note listing the attempts. `retry_on` is a predicate or a tuple of exception types.
    `sleep` defaults to time.sleep looked up at call time.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    if isinstance(retry_on, (type, tuple)):
        types = retry_on
        retry_on = lambda e: isinstance(e, types)  # noqa: E731
    start = _now()
    for n in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = n + 1 >= attempts
            if last or not retry_on(e):
                _note(e, "ras retry: gave up after %d attempt(s)" % (n + 1))
                raise
            delay = _rng.uniform(0.0, min(cap, base * (2 ** n)))
            ra = retry_after_s(e)
            if ra is not None:
                delay = max(delay, ra)
            if budget_s is not None and (_now() - start) + delay > budget_s:
                _note(e, "ras retry: stopped after %d attempt(s), budget %.3gs exhausted" % (n + 1, budget_s))
                raise
            if on_retry is not None:
                on_retry(n + 1, e, delay)
            (sleep or time.sleep)(delay)
    raise AssertionError("unreachable")


def _note(e: BaseException, text: str) -> None:
    if hasattr(e, "add_note"):
        try:
            e.add_note(text)
        except Exception:
            pass


# --------------------------------------------------------------------------- circuit breaker


class CircuitBreaker:
    """Opens after `fail_threshold` consecutive failures; after `cooldown_s` lets one trial call
    through (half-open): success closes it, failure re-opens it."""

    def __init__(self, fail_threshold: int = 5, cooldown_s: float = 30.0, name: str = "dependency",
                 count=is_transient):
        self.fail_threshold = fail_threshold
        self.cooldown_s = cooldown_s
        self.name = name
        self.count = count
        self.failures = 0
        self.opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if _now() - self.opened_at >= self.cooldown_s else "open"

    def reset(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = None

    def call(self, fn, *args, **kwargs):
        with self._lock:
            st = self.state
            if st == "open":
                raise CircuitOpenError("circuit open for %s" % self.name,
                                       context={"failures": self.failures, "cooldown_s": self.cooldown_s})
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            if self.count(e):
                with self._lock:
                    self.failures += 1
                    if st == "half_open" or self.failures >= self.fail_threshold:
                        self.opened_at = _now()
            raise
        with self._lock:
            self.failures = 0
            self.opened_at = None
        return result

    __call__ = call
