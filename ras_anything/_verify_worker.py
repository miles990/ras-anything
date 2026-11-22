"""verify, in its own process: replay recorded calls of the original and hardened function.

Usage: python -m ras_anything._verify_worker <harden_dir> <out.json> [max_chaos_calls]
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import pickle
import random
import sys
import time
import traceback

from . import boundary, faults, runtime
from .boundary import ReplayMiss
from .faults import ChaosHang

_perf = time.perf_counter  # real time, saved before the virtual clock is installed


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

    def perform(self, req, _live_serve):
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

        return super().perform(req, serve)

    def duplicates(self) -> int:
        groups: dict = {}
        for n, a in enumerate(self.attempts):
            if a["nonidempotent"] and a["reached"]:
                groups.setdefault(a["key"], set()).add(a["idem"] or ("<no key #%d>" % n))
        return sum(len(g) - 1 for g in groups.values())


def type_matches(exc: BaseException, recorded: str) -> bool:
    return any("%s.%s" % (c.__module__, c.__qualname__) == recorded for c in type(exc).__mro__)


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
        obj = getattr(obj, part)
    return mod, obj


def apply_globals(mod, snap):
    for k, v in (snap or {}).items():
        if k in vars(mod) and isinstance(vars(mod)[k], (str, int, float, bool, type(None))):
            setattr(mod, k, v)


def main(argv):
    hdir, out_path = argv[0], argv[1]
    max_chaos = int(argv[2]) if len(argv) > 2 else 60
    meta = json.load(open(os.path.join(hdir, "meta.json")))
    calls = pickle.load(open(os.path.join(hdir, "calls.pkl"), "rb"))
    hpath = os.path.join(hdir, "hardened.py")
    result = {"id": meta["id"], "budget_s": meta["budget_s"], "recorded": len(calls)}

    os.environ.update(meta.get("env") or {})
    os.environ["RAS_ANYTHING_DISABLE"] = "1"
    try:
        os.chdir(meta["cwd"])
    except OSError:
        pass
    sys.path[:0] = [p for p in meta.get("sys_path", []) if p not in sys.path]

    clock = faults.VirtualClock()
    handler = Replay(clock)
    boundary.install(handler)

    def fail(stage, msg):
        result.update({"passed": False, "fatal": {"stage": stage, "message": msg}})
        json.dump(result, open(out_path, "w"), default=str)
        return 0

    try:
        omod, original = load_original(meta)
        apply_globals(omod, meta.get("globals"))
    except BaseException:
        return fail("import original", traceback.format_exc(limit=6))
    try:
        spec = importlib.util.spec_from_file_location("_ras_hardened_" + meta["id"].replace(".", "__"), hpath)
        hmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hmod)
        hardened = getattr(hmod, meta["name"])
        apply_globals(hmod, meta.get("globals"))
    except BaseException:
        return fail("import hardened.py", traceback.format_exc(limit=6))
    fallback = getattr(hmod, "RAS_FALLBACK", None)

    budget = float(meta["budget_s"])
    runtime.VIRTUAL_CLOCK = True
    clock.install()
    real_stderr = sys.stderr

    def trial(fn, call, plan, seedval, fb):
        args, kwargs = pickle.loads(call["args"])
        handler.begin(call["io"], plan)
        random.seed(seedval)
        runtime.seed(seedval)
        clock.reset()
        err = io.StringIO()
        sys.stderr = err
        res = exc = None
        hang = miss = False
        try:
            res = fn(*args, **kwargs)
        except ChaosHang:
            hang = True
        except ReplayMiss as e:
            miss = str(e)
        except KeyboardInterrupt:
            raise
        except BaseException as e:
            exc = e
        finally:
            sys.stderr = real_stderr
            handler.active = False
        lat = clock.elapsed()
        if handler.hangs:
            hang = True
        if handler.misses and not miss:
            miss = "request the original never made: %s" % handler.misses[0]
        exp_exc = call.get("exc")
        expected = None if exp_exc else pickle.loads(call["result"])
        if hang or lat > budget + 1e-9:
            cls = "hang"
        elif miss:
            cls = "miss"
        elif exc is not None:
            cls = "ok" if exp_exc and type_matches(exc, exp_exc["type"]) else "error"
        elif exp_exc:
            cls = "degraded" if fb is not None and _eq(res, _fb(fb, args, kwargs)) else "wrong"
        elif _eq(res, expected):
            cls = "ok"
        elif fb is not None and _eq(res, _fb(fb, args, kwargs)):
            cls = "degraded"
        else:
            cls = "wrong"
        logs = sum(1 for ln in err.getvalue().splitlines() if '"trace_id"' in ln)
        structured = exc is not None and (isinstance(exc, runtime.RasError) or exc.__cause__ is not None)
        return {
            "class": cls, "latency": round(lat, 6), "dups": handler.duplicates(), "miss": miss or None,
            "result": None if exc is not None or hang else _short(res),
            "expected": _short(expected) if not exp_exc else "raises " + exp_exc["type"],
            "exc": None if exc is None else "%s: %s" % (type(exc).__name__, str(exc)[:200]),
            "logs": logs, "structured": structured,
            "attempts": [{"key": a["key"], "n": a["n"], "fault": a["fault"], "reached": a["reached"],
                          "idem": a["idem"], "method": a["method"]} for a in handler.attempts],
        }

    # (a) happy replay ---------------------------------------------------------------------------
    t0 = _perf()
    happy = {"total": 0, "equal": 0, "original_nondeterministic": 0, "first_failure": None}
    usable = []
    for i, call in enumerate(calls):
        o = trial(original, call, faults.Plan(), i, None)
        if o["class"] != "ok":
            happy["original_nondeterministic"] += 1
            continue
        usable.append(i)
        h = trial(hardened, call, faults.Plan(), i, None)
        happy["total"] += 1
        if h["class"] == "ok":
            happy["equal"] += 1
        elif happy["first_failure"] is None:
            happy["first_failure"] = {"call": i, "args": _args(call), **h}
    result["happy"] = happy

    # (b) chaos replay ---------------------------------------------------------------------------
    plans = [(k, None) for k in faults.MATRIX] + [("mixed", s) for s in (1, 2, 3)]
    chaos_idx = usable[:max_chaos]
    sides = {"original": [], "hardened": []}
    per_scn = {}
    first_bad = None
    first_dup = None
    for name, s in plans:
        row = per_scn.setdefault(name if s is None else "mixed", {"original": {}, "hardened": {}, "n": 0})
        for i in chaos_idx:
            call = calls[i]
            plan = faults.FirstAttempt(name) if s is None else faults.Seeded(s, 0.3, 0.0, salt="call%d" % i)
            seedval = int(faults._u(name, s, i) * 2 ** 32)
            o = trial(original, call, plan, seedval, None)
            h = trial(hardened, call, plan, seedval, fallback)
            row["n"] += 1
            for side, t in (("original", o), ("hardened", h)):
                sides[side].append(t)
                row[side][t["class"]] = row[side].get(t["class"], 0) + 1
            if o["dups"] and first_dup is None:
                first_dup = {"call": i, "args": _args(call), "scenario": name if s is None else "mixed seed %d" % s,
                             "original": o, "hardened": h}
            bad = h["class"] in ("wrong", "hang", "miss") or h["dups"] > o["dups"]
            if bad and first_bad is None:
                first_bad = {"call": i, "args": _args(call), "scenario": name if s is None else "mixed seed %d" % s,
                             "original": o, "hardened": h}
    clock.uninstall()
    result["chaos"] = {"calls": len(chaos_idx), "scenarios": [p[0] if p[1] is None else "mixed:%d" % p[1] for p in plans],
                       "by_scenario": per_scn, "first_failure": first_bad,
                       "first_original_dup": first_dup}
    for side, ts in sides.items():
        result[side] = summarize(ts, budget)
    o, h = result["original"], result["hardened"]
    gates = {
        "happy": happy["total"] > 0 and happy["equal"] == happy["total"],
        "no_wrong": h["wrong"] == 0 and h["miss"] == 0,
        "no_hang": h["hang"] == 0,
        "p99": h["p99"] is not None and h["p99"] <= budget,
        "dups": h["dups"] <= o["dups"],
        "availability": h["availability"] > o["availability"],
    }
    result["gates"] = gates
    result["passed"] = all(gates.values())
    result["fallback"] = None if fallback is None else ("callable" if callable(fallback) else _short(fallback))
    result["elapsed_s"] = round(_perf() - t0, 2)
    json.dump(result, open(out_path, "w"), default=str)
    return 0


def summarize(ts, budget):
    n = len(ts)
    c = {k: sum(1 for t in ts if t["class"] == k) for k in ("ok", "degraded", "wrong", "error", "hang", "miss")}
    lat = sorted(t["latency"] for t in ts)
    failures = [t for t in ts if t["class"] in ("error", "degraded")]
    errors = [t for t in ts if t["class"] == "error"]
    return {
        "trials": n, **c,
        "availability": (c["ok"] + c["degraded"]) / n if n else 0.0,
        "p50": lat[n // 2] if n else None,
        "p99": lat[min(n - 1, int(0.99 * n))] if n else None,
        "max": lat[-1] if n else None,
        "dups": sum(t["dups"] for t in ts),
        "logged_failures": (sum(1 for t in failures if t["logs"]) / len(failures)) if failures else None,
        "structured_errors": (sum(1 for t in errors if t["structured"]) / len(errors)) if errors else None,
    }


def _fb(fb, args, kwargs):
    if callable(fb):
        try:
            return fb(*args, **kwargs)
        except Exception:
            return object()
    return fb


def _eq(a, b) -> bool:
    try:
        return bool(a == b) and type(a) is type(b)
    except Exception:
        return False


def _short(v, n=300):
    r = repr(v)
    return r if len(r) <= n else r[: n - 1] + "…"


def _args(call):
    a, k = pickle.loads(call["args"])
    return _short(a, 200) + (" " + _short(k, 100) if k else "")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
