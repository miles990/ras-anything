"""Lab worker: runs simulated workloads through the real Policy code on a virtual event loop. It measures and
decides nothing (ras_anything/lab.py does). lang/node_agent/lab.cjs is the same simulation for the Node runtime.

Usage: python -m ras_anything.agents.python.lab <job.json>   (protocol: docs/PATTERNS.md, "How it is measured")
"""

from __future__ import annotations

import asyncio
import heapq
import io
import json
import math
import sys
import time
import urllib.error
from email.message import Message

from ... import runtime
from ...faults import _u
from ...runtime import Policy


class Clock:
    def __init__(self):
        self.t = 1000.0


class _Selector:
    """No files to watch: waiting moves the clock."""

    def __init__(self, clock):
        self.clock = clock

    def select(self, timeout=None):
        if timeout is None:
            raise RuntimeError("the simulation is stuck: nothing left to wait for")
        if timeout > 0:
            self.clock.t += timeout
        return []

    def register(self, *a):
        pass

    unregister = modify = register

    def get_key(self, fd):
        raise KeyError(fd)

    def get_map(self):
        return {}

    def close(self):
        pass


class Loop(asyncio.SelectorEventLoop):
    def __init__(self, clock):
        self.clock = clock
        super().__init__(_Selector(clock))

    def time(self):
        return self.clock.t


def http_error(code, retry_after=None):
    h = Message()
    if retry_after is not None:
        h["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("http://lab", code, "lab", h, io.BytesIO(b""))


class Dependency:
    """A server: some workers and a queue, a timeline of trouble, seeded faults, a rate limit. What it was asked
    to do costs it time even if the client has stopped waiting."""

    def __init__(self, name, spec, sim):
        self.name, self.spec, self.sim = name, spec, sim
        self.free = [0.0] * spec.get("workers", 0)
        self.starts: list[float] = []     # of queued jobs
        self.hits: list[float] = []       # admitted under the rate limit in this one-second window
        self.arrivals: list[tuple[float, int]] = []
        self.effects: dict = {}           # write operation -> keys (or attempt numbers) it was done under

    def phase(self, now):
        for p in self.spec.get("phases", []):
            if p["from"] <= now < p["to"]:
                return p
        return {}

    async def attempt(self, call, n, key):
        sim, spec = self.sim, self.spec
        now = sim.now()
        self.arrivals.append((now, call["id"]))
        if call["gave_up"]:
            sim.wasted += 1
        who = (sim.seed, self.name, call["id"], n)
        p = self.phase(now)
        mode = p.get("mode")
        if mode is None and spec.get("fault_rate") and _u(*who, "p") < spec["fault_rate"]:
            kinds = spec["kinds"]
            mode = kinds[int(_u(*who, "k") * len(kinds)) % len(kinds)]
        if mode == "refused":
            await asyncio.sleep(0.002)
            raise ConnectionRefusedError("lab")
        if mode == "hang":
            await asyncio.sleep(spec.get("hang_s", 60.0))
            raise ConnectionResetError("lab")
        if mode == "503":
            await asyncio.sleep(0.005)
            raise http_error(503)
        if spec.get("limit_rps"):
            self.hits = [t for t in self.hits if math.floor(t) == math.floor(now)]
            if len(self.hits) >= spec["limit_rps"]:
                await asyncio.sleep(0.002)
                raise http_error(429, 1)  # until the next window, in whole seconds
            self.hits.append(now)
        service = spec["service_s"] * (0.5 + _u(*who, "s")) * p.get("slower", 1.0)
        if mode == "slow":
            service += spec["slow_s"]
        wait = 0.0
        if self.free:
            self.starts = [t for t in self.starts if t > now]
            if len(self.starts) >= spec["queue"]:
                await asyncio.sleep(0.005)
                raise http_error(503)
            start = max(now, self.free[0])
            heapq.heapreplace(self.free, start + service)
            self.starts.append(start)
            wait = start - now
        if call["op"] == "write":
            self.effects.setdefault(call["id"], set()).add(key or "attempt %d" % n)
        if mode == "lost":  # done, but the answer never arrives
            await asyncio.sleep(spec.get("hang_s", 60.0))
            raise ConnectionResetError("lab")
        await asyncio.sleep(wait + service)
        return {"call": call["id"]}


class Sim:
    def __init__(self, scenario, patterns, seed, clock):
        self.sc, self.seed, self.clock, self.t0 = scenario, seed, clock, clock.t
        self.wasted = 0
        self.patience = scenario["patience_s"]
        self.deps = {n: Dependency(n, s, self) for n, s in scenario["deps"].items()}
        self.on = patterns
        self.policies = {}
        for n in self.deps:
            cfg = {k: dict(v) if isinstance(v, dict) else v for k, v in patterns.items()}
            for k, v in (scenario.get("params") or {}).get(n, {}).items():
                if cfg.get(k):
                    cfg[k] = {**(cfg[k] if isinstance(cfg[k], dict) else {}), **v}
            self.policies[n] = Policy(n, **cfg)
        self.slots = asyncio.Semaphore(scenario["slots"]) if scenario.get("slots") else None
        self.calls: list[dict] = []

    def now(self):
        return self.clock.t - self.t0

    async def ask(self, dep, call, stream):
        d, via = self.deps[dep], stream.get("via", {}).get(dep)

        async def fn(a):
            out = await d.attempt(call, a.n if dep == stream["dep"] else next(call["inner"]), a.idempotency_key)
            if via:
                await self.ask(via, call, stream)
            return out
        write = call["op"] == "write"
        return await self.policies[dep].acall(
            fn, method="POST" if write and not self.sc.get("naive_writes") else "GET",
            idempotency_key=runtime.idempotency_key("lab", call["id"]) if write else None,
            cache_key=stream.get("keys") and "k%d" % int(_u(self.seed, "key", call["id"]) * stream["keys"]))

    async def handle(self, call, stream):
        if self.slots:
            await self.slots.acquire()
        try:
            left = call["at"] + self.patience - self.now()
            if not self.on.get("deadline"):
                return await self.ask(stream["dep"], call, stream)
            with runtime.deadline(left):  # the caller's deadline came with the request
                return await self.ask(stream["dep"], call, stream)
        finally:
            if self.slots:
                self.slots.release()

    async def one(self, call, stream):
        task = asyncio.ensure_future(self.handle(call, stream))
        await asyncio.wait({task}, timeout=self.patience)
        call["latency"] = self.now() - call["at"]
        if not task.done():  # the caller leaves; what it started goes on
            call.update(gave_up=True, outcome="late")
            await asyncio.wait({task})
            task.exception()
            return
        e = task.exception()
        if e is not None:
            call["outcome"] = "error"
            if not isinstance(e, (OSError, runtime.RejectedError)):
                raise e
        else:
            r = task.result()
            call["outcome"] = "degraded" if r == "degraded" else "fresh" if r["call"] == call["id"] else "stale"

    async def main(self):
        import itertools

        tasks = []

        async def feed(s, stream):
            t, i = stream.get("from", 0.0), 0
            while True:
                if "burst" in stream:
                    if i >= stream["burst"]:
                        return
                else:
                    t += -math.log(1.0 - _u(self.seed, "arrive", s, i)) / stream["rps"]
                if t >= stream.get("to", self.sc["duration_s"]):
                    return
                i += 1
                await asyncio.sleep(max(0.0, t - self.now()))
                call = {"id": "%d.%d" % (s, i), "stream": s, "op": stream.get("op", "read"), "at": self.now(), "gave_up": False,
                        "inner": itertools.count(1)}
                self.calls.append(call)
                tasks.append(asyncio.ensure_future(self.one(call, stream)))

        await asyncio.gather(*(feed(s, x) for s, x in enumerate(self.sc["load"])))
        await asyncio.gather(*tasks)

    def result(self):
        return {"calls": [[c["stream"], round(c["at"], 6), c["outcome"], round(c["latency"], 6)] for c in self.calls],
                "arrivals": {n: [round(t, 6) for t, _ in d.arrivals] for n, d in self.deps.items()},
                "wasted": self.wasted, "dups": sum(len(k) - 1 for d in self.deps.values() for k in d.effects.values())}


def run_one(job):
    clock = Clock()
    real = time.monotonic, time.time
    time.monotonic, time.time = (lambda: clock.t), (lambda: 1.7e9 + clock.t)
    loop = Loop(clock)
    stderr, sys.stderr = sys.stderr, io.StringIO()
    try:
        runtime.reset_state()
        runtime.seed(job["seed"])
        asyncio.set_event_loop(loop)
        sim = Sim(job["scenario"], job["patterns"], job["seed"], clock)
        loop.run_until_complete(sim.main())
        return sim.result()
    finally:
        sys.stderr = stderr
        loop.close()
        asyncio.set_event_loop(None)
        time.monotonic, time.time = real


def main(argv) -> int:
    with open(argv[0], encoding="utf-8") as f:
        job = json.load(f)
    res = [run_one(r) for r in job["runs"]]
    with open(job["out"], "w", encoding="utf-8") as f:
        json.dump(res, f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
