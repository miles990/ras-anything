"""Runs inside the user's Python process (loaded by _inject/sitecustomize.py). Not for direct use.

Modes (from the JSON in RAS_ANYTHING_CFG; the wire format is docs/ARCHITECTURE.md):
  scan    count calls of the project's functions, attribute boundary calls to the innermost one
  chaos   inject seeded faults at the boundary (live), attribute each fault to a function
  record  record up to N calls of one function: args, result/exception and its boundary I/O
  bench   like chaos (or clean when seed is null) plus per-function stats for the hardened ids
Results go to cfg["out"] as JSON when the process exits.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import functools
import inspect
import json
import os
import platform
import sys
import threading
import time

from ... import cassette, faults, runtime
from . import boundary, values

CO_GEN = 0x20 | 0x100 | 0x200  # generator, iterable coroutine, async generator
CO_COROUTINE, CO_NESTED = 0x80, 0x10


def find_target(module, qualname):
    """(owner, what the owner holds, the plain function) for `name` or `Class.name`; function None if absent."""
    owner, attr = module, qualname
    if "." in attr:
        cls, attr = attr.split(".", 1)
        owner = getattr(module, cls, None)
    raw = vars(owner).get(attr) if owner is not None else None
    fn = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
    return owner, raw, fn if callable(fn) else None


def rebind(raw, fn):
    """fn, as the kind of attribute `raw` was."""
    return type(raw)(fn) if isinstance(raw, (staticmethod, classmethod)) else fn


class Agent:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mode = cfg["mode"]
        self.root = os.path.realpath(cfg["root"])
        self.exclude = [os.path.realpath(p) for p in cfg.get("exclude", [])]
        self.codec = values.Codec(cfg.get("project") or self.root, owns=self.owns_class)
        self._owned: dict = {}
        self.type_modules: set = set()
        self._user: dict = {}
        self._starts: dict = {}  # coroutine code -> f_lasti when it starts
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
            if self.in_project(real):
                k = (real, code.co_firstlineno, code.co_name)
        self._user[code] = k
        return k

    def owns_class(self, cls):
        """Is this a class of the project's own code (so its instances may be recorded)?"""
        ok = self._owned.get(cls)
        if ok is None:
            mod = sys.modules.get(cls.__module__)
            path = getattr(mod, "__file__", None)
            ok = self._owned[cls] = bool(path) and self.in_project(os.path.realpath(path))
        if ok:
            self.type_modules.add(cls.__module__)
        return ok

    def in_project(self, real):
        return (real == self.root or real.startswith(self.root + os.sep)) and \
            not any(real.startswith(x + os.sep) or real == x for x in self.exclude) and \
            "site-packages" not in real and "_ras_hardened" not in real and os.sep + ".ras" + os.sep not in real

    def innermost(self):
        """The project function a boundary call belongs to: the innermost one on the stack, helpers
        defined inside a function counting as that function."""
        f = sys._getframe(2)
        nested = None
        while f is not None:
            k = self.user_key(f.f_code)
            if k is not None:
                if not f.f_code.co_flags & CO_NESTED:
                    self.note_module(k, f.f_globals.get("__name__", ""))
                    return k
                nested = nested or (k, f.f_globals.get("__name__", ""))
            f = f.f_back
        if nested is None:
            return None
        self.note_module(*nested)
        return nested[0]

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
            if frame.f_code.co_flags & CO_COROUTINE and frame.f_lasti != self._starts.setdefault(frame.f_code, frame.f_lasti):
                return  # an await resuming, not a new call
            st["calls"] += 1
            if frame.f_code.co_flags & CO_GEN:
                st["gen"] = True
            if st["calls"] <= 3:
                code = frame.f_code
                n = code.co_argcount + code.co_kwonlyargcount + bool(code.co_flags & 0x04) + bool(code.co_flags & 0x08)
                loc = frame.f_locals
                ok = self.codec.can([loc.get(v) for v in code.co_varnames[:n]])
                st["arg_ok"] = ok if st["arg_ok"] is None else (st["arg_ok"] and ok)
        elif event == "return":
            k = self._user.get(frame.f_code)
            if not k or frame.f_code.co_flags & CO_COROUTINE:  # (a coroutine also "returns" at every await)
                return
            st = self.funcs.get(k)
            if st is not None and st["calls"] <= 3:
                ok = self.codec.can(arg)
                st["ret_ok"] = ok if st["ret_ok"] is None else (st["ret_ok"] and ok)

    # ---------------------------------------------------------------- record (settrace on one code object)

    def start_record(self):
        tgt = self.cfg["target"]
        self.recording: contextvars.ContextVar = contextvars.ContextVar("ras_recording", default=None)
        self.t_file = os.path.realpath(tgt["file"])
        self.t_line = tgt["line"]
        self.t_name = tgt["name"]
        self.max_calls = int(self.cfg.get("max_calls", 500))
        self.tls = threading.local()
        self.calls_path = self.cfg["calls_path"]
        self.target_seen = 0
        self.snapshot = None

        agent = self

        class Recorder(boundary.Handler):
            def after(self, req, out, dur):
                cur = getattr(agent.tls, "current", None) or agent.recording.get()
                if cur is None:
                    return
                try:
                    cur["io"].append({"req": agent.codec.enc(req.to_dict()),
                                      "out": agent.codec.enc(boundary.recorded(out)), "dur": round(dur, 6)})
                except Exception:  # an answer that isn't data: this call can't be replayed
                    cur["bad"] = True

        self.handler = Recorder()
        boundary.install(self.handler)
        if tgt.get("async"):
            boundary.on_import(tgt["module"], lambda module: self.wrap_record(module, tgt["qualname"]))
        else:
            sys.settrace(self.trace)
            threading.settrace(self.trace)

    def take_snapshot(self, g, code):
        if self.snapshot is None:
            simple = (str, int, float, bool, type(None))
            self.snapshot = {"env": self.codec.enc(dict(os.environ)),
                             "globals": {n: self.codec.enc(g[n]) for n in code.co_names if n in g and type(g[n]) in simple}}

    def wrap_record(self, module, qualname):
        """Coroutines are recorded by a wrapper: tracing can't tell their awaits from their end."""
        owner, raw, fn = find_target(module, qualname)
        if fn is None:
            return

        @functools.wraps(fn)
        async def wrapper(*a, **kw):
            self.target_seen += 1
            self.take_snapshot(fn.__globals__, fn.__code__)
            rec = None
            if len(self.records) < self.max_calls and self.recording.get() is None:
                try:
                    rec = {"args": self.codec.enc(list(a)), "kwargs": self.codec.enc(kw), "io": []}
                except Exception:
                    self.skipped += 1
            if rec is None:
                return await fn(*a, **kw)
            token = self.recording.set(rec)
            t0 = time.perf_counter()
            res = exc = None
            try:
                res = await fn(*a, **kw)
                return res
            except Exception as e:
                exc = e
                raise
            except BaseException:  # cancelled: not a result to compare with
                rec = None
                raise
            finally:
                self.recording.reset(token)
                if rec is not None:
                    self.keep(rec, t0, res, exc)

        setattr(owner, qualname.rsplit(".", 1)[-1], rebind(raw, wrapper))

    def keep(self, rec, t0, res, exc):
        rec["dur"] = round(time.perf_counter() - t0, 6)
        if rec.get("bad"):
            self.skipped += 1
            return
        try:
            if exc is not None:
                rec["exc"] = values.dump_exc(exc)
            else:
                rec["result"] = self.codec.enc(res)
            with self.lock:
                if len(self.records) < self.max_calls:
                    self.records.append(rec)
        except Exception:
            self.skipped += 1

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
        self.take_snapshot(frame.f_globals, frame.f_code)
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
                rec = {"args": self.codec.enc(args), "kwargs": self.codec.enc(kwargs), "io": [],
                       "t0": time.perf_counter()}
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
                self.keep(rec, rec.pop("t0"), arg, tls.pending_exc)
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

        def wrap_all(module):
            for t in by_mod[module.__name__]:
                agent.wrap_stats(module, t)

        for name in by_mod:
            boundary.on_import(name, wrap_all)

    def wrap_stats(self, module, t):
        owner, raw, fn = find_target(module, t["qualname"])
        if fn is None:
            return
        impl = fn
        for _ in range(6):
            nxt = getattr(impl, "__ras_hardened__", None) or getattr(impl, "__wrapped__", None)
            if nxt is None or nxt is impl:
                break
            impl = nxt
        st = self.stats.setdefault(t["id"], {"calls": 0, "ok": 0, "raised": {}, "fallback": 0, "lat": [],
                                             "attempts": 0, "outage_calls": 0, "outage_attempts": 0,
                                             "hardened": "_ras_hardened" in (getattr(impl, "__code__", None) and impl.__code__.co_filename or "")})
        fallback = getattr(impl, "__globals__", {}).get("RAS_FALLBACK")

        @contextlib.contextmanager
        def watch(a, kw):
            attempts: list = []  # what this call (and tasks/threads it hands its context to) asked of dependencies
            box: dict = {}
            token = boundary._scopes.set(boundary._scopes.get() + (attempts,))
            t0 = time.perf_counter()
            st["calls"] += 1
            try:
                yield box
            except BaseException as e:
                name = type(e).__name__
                st["raised"][name] = st["raised"].get(name, 0) + 1
                raise
            finally:
                boundary._scopes.reset(token)
                st["lat"].append(time.perf_counter() - t0)
                st["attempts"] += len(attempts)
                if any(x["outage"] for x in attempts):
                    st["outage_calls"] += 1
                    st["outage_attempts"] += len(attempts)
            fb = None
            try:
                fb = fallback(*a, **kw) if callable(fallback) else fallback
            except Exception:
                pass
            # a fallback only counts when this very call saw a request fail that never got a good answer
            answered = {x["key"] for x in attempts if not x["failed"]}
            if fallback is not None and box["res"] == fb and any(x["key"] not in answered for x in attempts):
                st["fallback"] += 1
            else:
                st["ok"] += 1

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*a, **kw):
                with watch(a, kw) as box:
                    box["res"] = await fn(*a, **kw)
                return box["res"]
        else:
            @functools.wraps(fn)
            def wrapper(*a, **kw):
                with watch(a, kw) as box:
                    box["res"] = fn(*a, **kw)
                return box["res"]

        setattr(owner, t["qualname"].rsplit(".", 1)[-1], rebind(raw, wrapper))

    # ---------------------------------------------------------------- start / finish

    def start(self):
        mode = self.mode
        if mode == "scan":
            agent = self

            class Counter(boundary.Handler):
                def before(self, req):
                    k = agent.innermost()
                    agent.boundary_total += 1
                    if k is not None:
                        st = agent.funcs[k]
                        st["boundary"] += 1
                        st.setdefault("kinds", {})
                        st["kinds"][req.kind] = st["kinds"].get(req.kind, 0) + 1

            self.handler = Counter()
            boundary.install(self.handler)
            sys.setprofile(self.profile)
            threading.setprofile(self.profile)
        elif mode in ("chaos", "bench"):
            seed = self.cfg.get("seed")
            if seed is not None:
                runtime.seed(seed)
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
            where = None
            t = tb
            while t is not None:  # innermost frame of the user's code
                k = self.user_key(t.tb_frame.f_code)
                if k is not None:
                    mod = t.tb_frame.f_globals.get("__name__", "")
                    where = "%s.%s" % (os.path.splitext(os.path.basename(k[0]))[0] if mod == "__main__" else mod,
                                       getattr(t.tb_frame.f_code, "co_qualname", k[2]))
                t = t.tb_next
            self.uncaught = {"type": tp.__name__, "msg": str(val)[:300], "hang": issubclass(tp, faults.ChaosHang),
                             "where": where}
            prev_hook(tp, val, tb)

        sys.excepthook = hook
        atexit.register(self.finish)

    def finish(self):
        sys.setprofile(None)
        sys.settrace(None)
        out = {"protocol": 1, "mode": self.mode, "uncaught": self.uncaught, "executable": sys.executable,
               "runtime": {"name": platform.python_implementation(), "version": platform.python_version()},
               "sys_path": [p for p in sys.path if "_inject" not in p and not p.startswith("__editable__")],
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
            cassette.save(self.calls_path, self.records, "python")
            out["recorded"] = len(self.records)
            out["seen"] = self.target_seen
            out["skipped"] = self.skipped
            out["snapshot"] = self.snapshot
            out["type_modules"] = sorted(self.type_modules)
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
