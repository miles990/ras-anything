"""Replay worker: runs trials of recorded calls, original or hardened, on a virtual clock, and reports
raw facts. It decides nothing: classification and gates are in ras_anything/judge.py, in another process.

Usage: python -m ras_anything.agents.python.worker <job.json>   (protocol: docs/ARCHITECTURE.md)
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import inspect
import io
import json
import os
import random
import sys
import traceback

from ... import cassette, faults, runtime
from . import boundary, values
from .boundary import ReplayMiss
from ...faults import ChaosHang, VirtualClock

PROTOCOL = 1


class Replay(boundary.FaultingHandler):
    """Serves dependency calls from one recorded call's cassette, with faults from a plan."""

    def __init__(self, clock):
        super().__init__(faults.Plan(), clock)
        self.io, self.used, self.misses = [], [], []
        self.active = False

    def begin(self, io_, plan):
        self.io = io_
        self.used = [False] * len(io_)
        self.plan = plan
        self.misses = []
        self.reset()
        self.active = True

    def match(self, req):
        last = None
        for i, x in enumerate(self.io):
            if x["req"]["key"] == req.key:
                if not self.used[i]:
                    return i
                last = i
        return last

    def source(self, req):
        """The recorded answer to this request, as a `serve` callable."""
        if not self.active:
            raise ReplayMiss("dependency call outside a replayed call: %s" % req.key)
        i = self.match(req)
        if i is None:
            self.misses.append(req.key)
            raise ReplayMiss("request the original never made: %s" % req.key)

        def serve():
            self.used[i] = True
            x = self.io[i]
            return x["out"], x.get("dur") or 0.0

        return serve

    def perform(self, req, _live_serve):
        return super().perform(req, self.source(req))

    async def aperform(self, req, _live_serve):
        recorded = self.source(req)

        async def serve():
            return recorded()

        return await super().aperform(req, serve)


def class_finder(meta):
    """Classes of the project's own modules that recording saw values of. Only modules this process
    has already imported are looked at: the cassette can't make anything run."""
    names = {"__main__": meta["module"]}

    def find(module, qualname):
        module = names.get(module, module)
        if module not in set(meta.get("type_modules") or []) | {meta["module"]}:
            raise LookupError("not one of the recorded project modules")
        obj = sys.modules.get(module)
        for part in qualname.split("."):
            obj = getattr(obj, part, None)
        if obj is None:
            raise LookupError("not defined there")
        return obj

    return find


def load_original(meta):
    if meta["in_main"]:
        spec = importlib.util.spec_from_file_location(meta["module"], meta["file"])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[meta["module"]] = mod
        spec.loader.exec_module(mod)
    else:
        mod = importlib.import_module(meta["module"])
    obj = mod
    for part in meta["qualname"].split("."):
        owner, obj = obj, inspect.getattr_static(obj, part)
    if isinstance(obj, (staticmethod, classmethod)):  # recorded arguments include self / cls
        obj = obj.__func__
    return mod, obj


def apply_globals(mod, snap):
    for k, v in (snap or {}).items():
        if k in vars(mod) and type(vars(mod)[k]) in (str, int, float, bool, type(None)):
            setattr(mod, k, v)


def plan_of(p):
    if p["type"] == "none":
        return faults.Plan()
    if p["type"] == "down":
        return faults.Down()
    if p["type"] == "first":
        return faults.FirstAttempt(p["fault"], target_key=p.get("key"))
    if p["type"] == "schedule":
        return faults.Schedule(p.get("faults") or [], target_key=p.get("key"), repeat=p.get("repeat", True))
    if p["type"] == "seeded":
        return faults.Seeded(p["seed"], p["rate"], p["outage_rate"], salt=p.get("salt", ""))
    raise ValueError("unknown plan %r" % (p["type"],))


class Fatal(Exception):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage, self.message = stage, message


def run_job(job: dict) -> dict:
    meta, project = job["meta"], job["project"]
    codec = values.Codec(project, find=class_finder(meta))
    aliases = {"__main__": meta["module"]}
    meta = dict(meta, file=os.path.normpath(os.path.join(project, meta["file"])))
    os.environ.update(codec.dec(meta.get("env") or {}))
    os.environ["RAS_ANYTHING_DISABLE"] = "1"
    try:
        os.chdir(os.path.join(project, meta.get("cwd") or "."))
    except OSError:
        pass
    paths = [os.path.normpath(os.path.join(project, p)) for p in meta.get("sys_path", [])]
    sys.path[:0] = [p for p in paths if p not in sys.path]

    clock = VirtualClock()
    handler = Replay(clock)
    boundary.install(handler)
    glob = codec.dec(meta.get("globals") or {})
    try:
        omod, original = load_original(meta)
        apply_globals(omod, glob)
        for m in meta.get("type_modules") or []:
            if m != "__main__":
                importlib.import_module(m)
    except BaseException:
        raise Fatal("import original", traceback.format_exc(limit=6))
    try:
        calls = cassette.load(job["cassette"])
        for call in calls:
            call["io"] = codec.dec(call["io"])
            codec.dec([call["args"], call["kwargs"], call.get("result")])
    except (cassette.BadCassette, OSError, KeyError, TypeError, ValueError) as e:
        raise Fatal("read %s" % os.path.basename(job["cassette"]), "%s: %s" % (type(e).__name__, e))
    try:
        spec = importlib.util.spec_from_file_location("_ras_hardened_" + meta["id"].replace(".", "__"), job["hardened"])
        hmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hmod)
        hardened = getattr(hmod, meta["name"])
        apply_globals(hmod, glob)
    except BaseException:
        raise Fatal("import %s" % os.path.basename(job["hardened"]), traceback.format_exc(limit=6))
    fallback = getattr(hmod, "RAS_FALLBACK", None)
    deps = job.get("dependencies") or cassette.dependencies(calls)
    watched = runtime.observed(meta["id"], hardened, deps, original)

    runtime.VIRTUAL_CLOCK = clock
    clock.install()
    real_stderr = sys.stderr

    def trial(t):
        call = calls[t["call"]]
        fn = watched if t["side"] == "hardened" else original
        args, kwargs = codec.dec(call["args"]), codec.dec(call["kwargs"])
        handler.begin(call["io"], plan_of(t["plan"]))
        random.seed(t["seed"])
        runtime.seed(t["seed"])
        if t.get("gap_s"):  # one of a sequence: time passes in between, and what the code learned stays
            clock.reset()
            clock.wait(t["gap_s"])
        else:
            runtime.reset_state()
        clock.reset()
        err = io.StringIO()
        sys.stderr = err
        res = exc = None
        outcome, miss = "return", None
        try:
            res = fn(*args, **kwargs)
            if inspect.iscoroutine(res):
                res = asyncio.run(res)
        except ChaosHang:
            outcome = "hang"
        except ReplayMiss as e:
            outcome, miss = "miss", str(e)
        except KeyboardInterrupt:
            raise
        except BaseException as e:
            outcome, exc = "raise", e
        finally:
            sys.stderr = real_stderr
            handler.active = False
        latency = clock.elapsed()
        if handler.hangs:
            outcome = "hang"
        elif handler.misses:
            outcome, miss = "miss", miss or "request the original never made: %s" % handler.misses[0]
        exp_exc = call.get("exc")
        expected = None if exp_exc else codec.dec(call["result"])
        if outcome == "raise":
            matches = bool(exp_exc) and values.matches(exc, exp_exc, aliases)
        else:
            matches = outcome == "return" and not exp_exc and _eq(res, expected)
        cause = getattr(exc, "__cause__", None)
        return {
            "outcome": outcome, "latency": round(latency, 6), "matches": matches,
            "is_fallback": outcome == "return" and fallback is not None and _eq(res, _fallback(
                fallback, codec.dec(call["args"]), codec.dec(call["kwargs"]))),
            "miss": miss, "logs": sum(1 for ln in err.getvalue().splitlines() if '"trace_id"' in ln),
            "result": _short(res) if outcome == "return" else None,
            "expected": "raises %s.%s" % (exp_exc["module"], exp_exc["type"]) if exp_exc else _short(expected),
            "exc": None if exc is None else "%s: %s" % (type(exc).__name__, str(exc)[:200]),
            "exc_type": None if exc is None else type(exc).__name__,
            "cause_type": None if cause is None else type(cause).__name__,
            "structured": exc is not None and (isinstance(exc, runtime.RasError) or cause is not None),
            "problem": exc.problem if isinstance(exc, runtime.RasError) else None,
            "stderr": err.getvalue()[:8000],
            "state": _state(codec, meta, args, kwargs) if t["plan"]["type"] == "none" else None,
            "attempts": [{k: a[k] for k in ("key", "n", "fault", "reached", "failed", "idem", "method", "nonidempotent", "outage")}
                         for a in handler.attempts],
        }

    try:
        results = [trial(t) for t in job["trials"]]
    finally:
        clock.uninstall()
    return {"results": results, "fallback": None if fallback is None else ("callable" if callable(fallback) else _short(fallback))}


# --------------------------------------------------------------------------- helpers

_structure = values.Codec(owns=lambda cls: True)


def _eq(a, b) -> bool:
    """Equal and of the same type; or, for classes without __eq__, equal attribute by attribute."""
    try:
        if type(a) is not type(b):
            return False
        return bool(a == b) or _structure.enc(a) == _structure.enc(b)
    except Exception:
        return False


def _fallback(fb, args, kwargs):
    if callable(fb):
        try:
            return fb(*args, **kwargs)
        except Exception:
            return object()
    return fb


def _digest(v) -> str:
    try:
        text = json.dumps(_structure.enc(v), sort_keys=True)
    except Exception:
        return "?"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _state(codec, meta, args, kwargs) -> dict:
    """What the call left behind in its arguments (and, for a method, in each attribute of self)."""
    if meta.get("kind") == "method" and args:
        me = {k: _digest(v) for k, v in getattr(args[0], "__dict__", {}).items()}
        return {"args": _digest([args[1:], kwargs]), "self": me}
    return {"args": _digest([args, kwargs])}


def _short(v, n=300):
    try:
        r = repr(v)
    except Exception as e:
        r = "<%s whose repr raises %s>" % (type(v).__name__, type(e).__name__)
    return r if len(r) <= n else r[: n - 1] + "…"


def main(argv) -> int:
    with open(argv[0], encoding="utf-8") as f:
        job = json.load(f)
    try:
        res = run_job(job)
    except Fatal as e:
        res = {"fatal": {"stage": e.stage, "message": e.message}}
    with open(job["out"], "w", encoding="utf-8") as f:
        json.dump({"protocol": PROTOCOL, **res}, f, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
