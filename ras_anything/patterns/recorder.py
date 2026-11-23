"""Flight recorder: what the last calls of hardened functions did, in memory; written out as an incident when one of them
could not be answered (raised, or gave up on a dependency).

Bounded: 64 calls in memory, 200 events per call, 5 incidents per kind of failure and 100 per process, the newest 200
files in the folder. Arguments are kept redacted (secrets and personal data by name and by shape) plus a hash of the
whole, which is how `ras-anything replay` finds the recorded call to repeat. Nothing is written unless there is a
.ras/ folder (or $RAS_ANYTHING_INCIDENTS names one); $RAS_ANYTHING_INCIDENTS=off switches it off.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import re
import threading
import time
from collections import deque

from .. import runtime as rt

RING, BEFORE, PER_KIND, PER_PROCESS, KEEP_FILES = 64, 20, 5, 100, 200
_ring: deque = deque(maxlen=RING)
_written: dict = {}
_lock = threading.Lock()

# --------------------------------------------------------------------------- inputs

_SENSITIVE = frozenset("""password passwd pwd secret token auth authorization credential credentials apikey key session cookie signature pin otp cvv
cvc card pan iban account ssn passport dob birthday birthdate email mail phone mobile tel fax address street zip postcode city name firstname
lastname fullname surname username user customer patient recipient ip""".split())
_PUBLIC = frozenset("""idempotency_key key_id sort_key cache_key primary_key filename file_name hostname app_name service_name city_id user_id
customer_id account_id""".split())


def _parts(name: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name)).lower()) if w]


def sensitive(name) -> bool:
    """Is a value with this name one to keep out of the record?"""
    words = _parts(name)
    joined = "_".join(words)
    extra = [w.strip().lower() for w in os.environ.get("RAS_ANYTHING_REDACT", "").split(",") if w.strip()]
    if joined in extra or any(w in extra for w in words):
        return True
    return joined not in _PUBLIC and any(w in _SENSITIVE for w in words)


def redact(value, name=None, depth: int = 0):
    """A copy of a value that may be written down: sensitive names are dropped, text is masked, size is bounded."""
    if name is not None and sensitive(name) and value is not None:
        return "[redacted]"
    if isinstance(value, (bool, int, float, type(None))):
        if isinstance(value, int) and not isinstance(value, bool):
            return value if rt.mask(str(value)) == str(value) else "[redacted]"
        return value
    if isinstance(value, str):
        text = rt.mask(value)
        return text if len(text) <= 200 else "%s… (%d characters)" % (text[:200], len(text))
    if isinstance(value, (bytes, bytearray)):
        return "<%d bytes>" % len(value)
    if depth >= 4:
        return "<%s>" % type(value).__name__
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= 50:
                out["…"] = "%d more" % (len(value) - 50)
                break
            out[rt.mask(str(k))] = redact(v, k, depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        return [redact(v, name, depth + 1) for v in items[:50]] + (["… %d more" % (len(items) - 50)] if len(items) > 50 else [])
    attrs = getattr(value, "__dict__", None)
    if attrs:
        return {"<%s>" % type(value).__name__: {k: redact(v, k, depth + 1) for k, v in list(attrs.items())[:50]}}
    return "<%s>" % type(value).__name__


def _canon(v, depth: int = 0):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() and abs(v) < 2 ** 53 else repr(v)
    if isinstance(v, (bytes, bytearray)):
        return {"b": base64.b64encode(bytes(v)).decode()}
    if depth > 8:
        return type(v).__name__
    if isinstance(v, dict):
        return {str(k): _canon(x, depth + 1) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, (list, tuple)):
        return [_canon(x, depth + 1) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted(json.dumps(_canon(x, depth + 1), sort_keys=True) for x in v)
    attrs = getattr(v, "__dict__", None)
    return {type(v).__name__: _canon(attrs, depth + 1) if attrs else str(v)}


def bound(fn, args, kwargs):
    """The arguments by parameter name, however they were passed."""
    try:
        ba = inspect.signature(fn).bind(*args, **kwargs)
        ba.apply_defaults()
        return dict(ba.arguments)
    except (TypeError, ValueError):
        return {**{"arg%d" % i: a for i, a in enumerate(args)}, **kwargs}


def input_hash(fn, args, kwargs) -> str:
    try:
        text = json.dumps(_canon(list(bound(fn, args, kwargs).values())), sort_keys=True, ensure_ascii=True)
    except Exception:
        return "?"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- recording


def finish(ctx, outcome: str, error, result) -> str | None:
    """A hardened call has ended (called by the runtime). Returns the id of the incident it left, if any."""
    broken = outcome == "raise" and not isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)) and type(error).__name__ != "CancelledError"
    entry = {"fid": ctx.fid, "at": rt._now(), "duration_s": round(rt._now() - ctx.t0, 3), "outcome": "raise" if broken else outcome}
    if ctx.events:
        entry["events"], entry["input_hash"] = ctx.events, input_hash(ctx.fn, ctx.args, ctx.kwargs)
        entry["fault"], entry["seed"] = ctx.gave_up, ctx.seed
    iid = None
    if broken or ctx.gave_up is not None:
        hang = broken and not isinstance(error, Exception)
        iid = _dump(ctx, "hang" if hang else "raised" if broken else "gave_up", outcome, error, result)
    _ring.append(entry)
    return iid


def folder() -> str | None:
    at = os.environ.get("RAS_ANYTHING_INCIDENTS")
    if at:
        return None if at.lower() in ("off", "0", "no") else at
    if rt.VIRTUAL_CLOCK is not None:  # verify replays faults on purpose
        return None
    ras = rt._flag_path().parent
    return str(ras / "incidents") if ras.name == ".ras" and ras.is_dir() else None


def dump_incident(reason: str = "manual") -> str | None:
    """Write out the call in progress now (from inside hardened code). Returns the incident id."""
    ctx = rt._ctx.get()
    try:
        return _dump(ctx, reason, "running", None, None) if ctx is not None else None
    except Exception:
        return None


def _dump(ctx, trigger, outcome, error, result):
    at = folder()
    if at is None:
        return None
    problem = None
    if error is not None:
        try:
            problem = rt._lazy("problem", "problem")(error)
        except Exception:
            pass
    fault = ctx.gave_up or (rt.fault_class(error) if error is not None else None)
    kind = (ctx.fid, ctx.last_dependency, fault, trigger)
    with _lock:
        if _written.get(kind, 0) >= PER_KIND or sum(_written.values()) >= PER_PROCESS:
            return None
        _written[kind] = _written.get(kind, 0) + 1
    now, wall = rt._now(), time.time()
    before = [{"age_s": round(ctx.t0 - (e["at"] - e["duration_s"]), 3), **{k: v for k, v in e.items() if k not in ("fid", "at")}}
              for e in list(_ring) if e["fid"] == ctx.fid][-BEFORE:]
    iid = "inc-%s-%s-%s" % (time.strftime("%Y%m%dT%H%M%S", time.gmtime(wall)), re.sub(r"[^A-Za-z0-9_.-]", "_", ctx.fid)[:60], ctx.span_id[:8])
    doc = {
        "schema_version": "1.0.0", "kind": "incident", "id": iid, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall)), "lang": "python",
        "service": rt._SERVICE, "pid": os.getpid(), "function": ctx.fid, "trigger": trigger, "trace_id": ctx.trace_id, "span_id": ctx.span_id,
        "parent_span_id": ctx.parent, "seed": ctx.seed, "started": round(ctx.ts, 3), "duration_s": round(now - ctx.t0, 3),
        "inputs": redact(bound(ctx.fn, ctx.args, ctx.kwargs)), "input_hash": input_hash(ctx.fn, ctx.args, ctx.kwargs),
        "dependencies": ctx.deps, "dependency": ctx.last_dependency, "fault": fault,
        "outcome": {"kind": outcome, "error_type": None if error is None else type(error).__name__,
                    "message": None if error is None else rt.mask(str(error))[:300], "problem": problem},
        "events": ctx.events, "events_dropped": ctx.dropped, "guards_at_start": ctx.health or [], "before": before,
    }
    os.makedirs(at, exist_ok=True)
    tmp = os.path.join(at, ".%s.tmp" % iid)
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1, default=str)
    os.replace(tmp, os.path.join(at, iid + ".json"))
    old = sorted(n for n in os.listdir(at) if n.startswith("inc-") and n.endswith(".json"))[:-KEEP_FILES]
    for n in old:
        os.unlink(os.path.join(at, n))
    rt.emit("ras.incident", "ERROR", "incident %s written" % iid, {"ras.incident.id": iid, "ras.fault": fault, "ras.dependency": ctx.last_dependency})
    return iid


def reset() -> None:
    """Forget what is in memory (tests)."""
    _ring.clear()
    _written.clear()
