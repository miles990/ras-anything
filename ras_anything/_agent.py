"""Runs inside the user's Python process (loaded by _inject/sitecustomize.py). Not for direct use.

Modes (from the JSON in RAS_ANYTHING_CFG):
  scan    count calls of the project's functions, attribute boundary calls to the innermost one
  chaos   inject seeded faults at the boundary (live), attribute each fault to a function
  record  record up to N calls of one function: args, result/exception and its boundary I/O
  bench   like chaos (or clean when seed is null) plus per-function stats for the hardened ids
Results go to cfg["out"] as JSON when the process exits.
"""

from __future__ import annotations

import atexit
import functools
import importlib.abc
import json
import os
import pickle
import sys
import threading
import time

from . import boundary, faults

CO_GEN = 0x20 | 0x80 | 0x100 | 0x200  # generator, coroutine, iterable coroutine, async generator


class Agent:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mode = cfg["mode"]
        self.root = os.path.realpath(cfg["root"])
        self.exclude = [os.path.realpath(p) for p in cfg.get("exclude", [])]
        self.main_file = None
        self._user: dict = {}
        self.funcs: dict = {}
        self.boundary_by_fn: dict = {}
        self.boundary_total = 0
        self.uncaught = None
        self.records: list = []
        self.skipped = 0
        self.stats: dict = {}
        self.lock = threading.Lock()
        self.handler = None

    # ---------------------------------------------------------------- attribution

    def user_key(self, code):
        k = self._user.get(code, False)
        if k is not False:
            return k
        fn = code.co_filename
        k = None
        if fn and not fn.startswith("<"):
            real = os.path.realpath(fn)
            if (real == self.root or real.startswith(self.root + os.sep)) and \
                    not any(real.startswith(x + os.sep) or real == x for x in self.exclude) and \
                    "site-packages" not in real and "_ras_hardened" not in real and os.sep + ".ras" + os.sep not in real:
                k = (real, code.co_firstlineno, code.co_name)
        self._user[code] = k
        return k

    def innermost(self):
        f = sys._getframe(2)
        while f is not None:
            k = self.user_key(f.f_code)
            if k is not None:
                mod = f.f_globals.get("__name__", "")
                self.note_module(k, mod)
                return k
            f = f.f_back
        return None

    def note_module(self, k, mod):
        st = self.funcs.get(k)
        if st is None:
            st = self.funcs[k] = {"calls": 0, "module": mod, "gen": False, "arg_ok": None, "ret_ok": None,
                                  "boundary": 0, "faults": {}}
        elif not st.get("module"):
            st["module"] = mod
        return st

    # ---------------------------------------------------------------- profiler (scan)

    def profile(self, frame, event, arg):
        if event == "call":
            k = self.user_key(frame.f_code)
            if k is None:
                return
            st = self.note_module(k, frame.f_globals.get("__name__", ""))
            st["calls"] += 1
            if frame.f_code.co_flags & CO_GEN:
                st["gen"] = True
            if st["calls"] <= 3:
                code = frame.f_code
                n = code.co_argcount + code.co_kwonlyargcount + bool(code.co_flags & 0x04) + bool(code.co_flags & 0x08)
                try:
                    pickle.dumps(tuple(frame.f_locals.get(v) for v in code.co_varnames[:n]))
                    ok = True
                except Exception:
                    ok = False
                st["arg_ok"] = ok if st["arg_ok"] is None else (st["arg_ok"] and ok)
        elif event == "return":
            k = self._user.get(frame.f_code)
            if not k:
                return
            st = self.funcs.get(k)
            if st is not None and st["calls"] <= 3:
                try:
                    pickle.dumps(arg)
                    ok = True
                except Exception:
                    ok = False
                st["ret_ok"] = ok if st["ret_ok"] is None else (st["ret_ok"] and ok)

    # ---------------------------------------------------------------- record (settrace on one code object)

    def start_record(self):
        tgt = self.cfg["target"]
        self.t_file = os.path.realpath(tgt["file"])
        self.t_line = tgt["line"]
        self.t_name = tgt["name"]
        self.max_calls = int(self.cfg.get("max_calls", 500))
        self.tls = threading.local()
        self.calls_path = self.cfg["calls_path"]
        self.target_seen = 0

        agent = self

        class Recorder(boundary.Handler):
            def perform(self, req, serve):
                out, dur = serve()
                cur = getattr(agent.tls, "current", None)
                if cur is not None:
                    o = out
                    if "exc" in o:
                        o = {"exc": o["exc"]}
                    cur["io"].append({"req": req.to_dict(), "out": o, "dur": round(dur, 6)})
                return out

        self.handler = Recorder()
        boundary.install(self.handler)
        sys.settrace(self.trace)
        threading.settrace(self.trace)

    def is_target(self, code):
        k = self._user.get(code, False)
        if k is False:
            k = code.co_filename and os.path.realpath(code.co_filename) == self.t_file and \
                code.co_firstlineno == self.t_line and code.co_name == self.t_name
            self._user[code] = k
        return k

    def trace(self, frame, event, arg):
        if event != "call" or not self.is_target(frame.f_code):
            return None
        tls = self.tls
        depth = getattr(tls, "depth", 0)
        tls.depth = depth + 1
        if depth > 0:
            return self._local_depth_only
        self.target_seen += 1
        rec = None
        if len(self.records) < self.max_calls:
            code = frame.f_code
            n = code.co_argcount + code.co_kwonlyargcount
            names = code.co_varnames[:code.co_argcount]
            kwonly = code.co_varnames[code.co_argcount:n]
            loc = frame.f_locals
            args = [loc[v] for v in names]
            kwargs = {v: loc[v] for v in kwonly}
            i = n
            if code.co_flags & 0x04:
                args += list(loc[code.co_varnames[i]])
                i += 1
            if code.co_flags & 0x08:
                kwargs.update(loc[code.co_varnames[i]])
            try:
                rec = {"args": pickle.dumps((tuple(args), kwargs)), "io": [], "t0": time.perf_counter()}
            except Exception:
                self.skipped += 1
                rec = None
        tls.current = rec
        tls.pending_exc = None
        return self._local

    def _local_depth_only(self, frame, event, arg):
        if event == "return":
            self.tls.depth -= 1
        return self._local_depth_only

    def _local(self, frame, event, arg):
        tls = self.tls
        if event == "exception":
            tls.pending_exc = arg[1]
        elif event == "line":
            tls.pending_exc = None
        elif event == "return":
            tls.depth -= 1
            rec = tls.current
            tls.current = None
            if rec is not None:
                rec["dur"] = round(time.perf_counter() - rec.pop("t0"), 6)
                exc = tls.pending_exc
                try:
                    if exc is not None:
                        rec["exc"] = boundary.ser_exc(exc)
                    else:
                        rec["result"] = pickle.dumps(arg)
                    with self.lock:
                        if len(self.records) < self.max_calls:
                            self.records.append(rec)
                except Exception:
                    self.skipped += 1
            tls.pending_exc = None
        return self._local

    # ---------------------------------------------------------------- bench stats (wrap hardened ids)

    def start_bench_stats(self):
        targets = self.cfg.get("targets") or []
        by_mod: dict = {}
        for t in targets:
            if t.get("module") and not t.get("in_main"):
                by_mod.setdefault(t["module"], []).append(t)
        if not by_mod:
            return
        agent = self

        class Finder(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name not in by_mod:
                    return None
                for f in sys.meta_path:
                    if f is self or not hasattr(f, "find_spec"):
                        continue
                    spec = f.find_spec(name, path, target)
                    if spec is not None:
                        break
                else:
                    return None
                loader = spec.loader
                orig_exec = loader.exec_module

                def exec_module(module):
                    orig_exec(module)
                    for t in by_mod[name]:
                        agent.wrap_stats(module, t)

                loader.exec_module = exec_module
                return spec

        sys.meta_path.insert(0, Finder())

    def wrap_stats(self, module, t):
        owner, attr = module, t["qualname"]
        if "." in attr:
            cls, attr = attr.split(".", 1)
            owner = getattr(module, cls, None)
        fn = getattr(owner, attr, None)
        if fn is None or not callable(fn):
            return
        st = self.stats.setdefault(t["id"], {"calls": 0, "ok": 0, "raised": {}, "fallback": 0, "lat": [],
                                             "hardened": "_ras_hardened" in (getattr(fn, "__code__", None) and fn.__code__.co_filename or "")})
        fallback = getattr(fn, "__globals__", {}).get("RAS_FALLBACK")

        @functools.wraps(fn)
        def wrapper(*a, **kw):
            t0 = time.perf_counter()
            st["calls"] += 1
            try:
                res = fn(*a, **kw)
            except BaseException as e:
                name = type(e).__name__
                st["raised"][name] = st["raised"].get(name, 0) + 1
                st["lat"].append(time.perf_counter() - t0)
                raise
            st["lat"].append(time.perf_counter() - t0)
            fb = None
            try:
                fb = fallback(*a, **kw) if callable(fallback) else fallback
            except Exception:
                pass
            if fallback is not None and res == fb:
                st["fallback"] += 1
            else:
                st["ok"] += 1
            return res

        if owner is not module and isinstance(owner.__dict__.get(attr), staticmethod):
            setattr(owner, attr, staticmethod(wrapper))
        else:
            setattr(owner, attr, wrapper)

    # ---------------------------------------------------------------- start / finish

    def start(self):
        mode = self.mode
        if mode == "scan":
            agent = self

            class Counter(boundary.Handler):
                def perform(self, req, serve):
                    k = agent.innermost()
                    agent.boundary_total += 1
                    if k is not None:
                        st = agent.funcs[k]
                        st["boundary"] += 1
                        st.setdefault("kinds", {})
                        st["kinds"][req.kind] = st["kinds"].get(req.kind, 0) + 1
                    out, _ = serve()
                    return out

            self.handler = Counter()
            boundary.install(self.handler)
            sys.setprofile(self.profile)
            threading.setprofile(self.profile)
        elif mode in ("chaos", "bench"):
            seed = self.cfg.get("seed")
            plan = faults.Seeded(seed, self.cfg.get("rate", 0.2), self.cfg.get("outage_rate", 0.05)) \
                if seed is not None else faults.Plan()
            clock = faults.LiveClock(self.cfg.get("scale", 0.05), self.cfg.get("hang_s", 2.0))
            agent = self

            class Live(boundary.FaultingHandler):
                def on_attempt(self, req, rec):
                    k = agent.innermost()
                    agent.boundary_total += 1
                    if k is not None:
                        st = agent.funcs[k]
                        st["boundary"] += 1
                        if rec["fault"]:
                            st["faults"][rec["fault"]] = st["faults"].get(rec["fault"], 0) + 1
                        rec["fn"] = list(k)

            self.handler = Live(plan, clock)
            boundary.install(self.handler)
            if mode == "bench":
                self.start_bench_stats()
        elif mode == "record":
            self.start_record()
        else:
            raise ValueError("unknown mode %r" % mode)

        prev_hook = sys.excepthook

        def hook(tp, val, tb):
            self.uncaught = {"type": tp.__name__, "msg": str(val)[:300], "hang": issubclass(tp, faults.ChaosHang)}
            prev_hook(tp, val, tb)

        sys.excepthook = hook
        atexit.register(self.finish)

    def finish(self):
        sys.setprofile(None)
        sys.settrace(None)
        out = {"mode": self.mode, "uncaught": self.uncaught, "sys_path": [p for p in sys.path if "_inject" not in p],
               "main_file": os.path.realpath(sys.argv[0]) if sys.argv and sys.argv[0] else None,
               "boundary_total": self.boundary_total}
        out["functions"] = [
            {"file": k[0], "line": k[1], "name": k[2], **{kk: vv for kk, vv in st.items()}}
            for k, st in self.funcs.items()
        ]
        h = self.handler
        if isinstance(h, boundary.FaultingHandler):
            out["attempts"] = h.attempts
            out["hangs"] = h.hangs
        if self.mode == "record":
            with open(self.calls_path, "wb") as f:
                pickle.dump(self.records, f, protocol=4)
            out["recorded"] = len(self.records)
            out["seen"] = self.target_seen
            out["skipped"] = self.skipped
        if self.stats:
            for st in self.stats.values():
                lat = sorted(st.pop("lat"))
                st["p50"] = lat[len(lat) // 2] if lat else None
                st["p99"] = lat[min(len(lat) - 1, int(0.99 * len(lat)))] if lat else None
            out["stats"] = self.stats
        try:
            with open(self.cfg["out"], "w") as f:
                json.dump(out, f, default=str)
        except Exception as e:
            sys.stderr.write("ras-anything: couldn't write results: %r\n" % (e,))


def start():
    raw = os.environ.pop("RAS_ANYTHING_CFG", None)
    if not raw:
        return
    cfg = json.loads(raw)
    Agent(cfg).start()
