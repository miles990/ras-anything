// Lab worker: runs simulated workloads through the real Policy code on the virtual clock. It measures and decides
// nothing (ras_anything/lab.py does). agents/python/lab.py is the same simulation for the Python runtime.
//
// Usage: node lab.cjs <job.json>   (protocol: docs/PATTERNS.md, "How it is measured")
"use strict";
const fs = require("node:fs");
const faults = require("./faults.cjs");

const { u } = faults;
const tick = () => new Promise((r) => faults.real.setImmediate(r));

/** The virtual clock with its timers in a heap: a simulation has thousands. */
class Clock extends faults.VirtualClock {
  constructor() {
    super();
    const heap = [];
    const before = (a, b) => a.at < b.at || (a.at === b.at && a.seq < b.seq);
    this.heap = heap;
    const live = new Set();
    this.timers = {
      add: (timer) => {
        const x = { at: timer.at, seq: timer.seq, timer };
        timer.entry = x;
        live.add(timer);
        heap.push(x);
        for (let i = heap.length - 1; i > 0;) {
          const p = (i - 1) >> 1;
          if (!before(heap[i], heap[p])) break;
          [heap[i], heap[p]] = [heap[p], heap[i]];
          i = p;
        }
      },
      delete: (timer) => live.delete(timer),
      clear: () => {
        live.clear();
        heap.length = 0;
      },
      [Symbol.iterator]: () => live[Symbol.iterator](),
    };
    this.live = live;
  }

  pop() {
    const { heap } = this;
    const top = heap[0];
    const last = heap.pop();
    if (heap.length) {
      heap[0] = last;
      for (let i = 0; ;) {
        let m = 2 * i + 1;
        if (m >= heap.length) break;
        const r = m + 1;
        if (r < heap.length && (heap[r].at < heap[m].at || (heap[r].at === heap[m].at && heap[r].seq < heap[m].seq))) m = r;
        if (heap[i].at < heap[m].at || (heap[i].at === heap[m].at && heap[i].seq < heap[m].seq)) break;
        [heap[i], heap[m]] = [heap[m], heap[i]];
        i = m;
      }
    }
    return top;
  }

  advance() {
    while (this.heap.length) {
      const x = this.pop();
      if (!this.live.has(x.timer) || x.timer.entry !== x) continue;
      this.live.delete(x.timer);
      this.t = Math.max(this.t, x.at);
      x.timer.fn(...x.timer.args);
      return true;
    }
    return false;
  }
}

const clock = new Clock();
clock.install();
const rt = require("./runtime.cjs");

const sleep = (s) => new Promise((r) => setTimeout(r, s * 1000));
const refused = () => Object.assign(new Error("lab"), { code: "ECONNREFUSED" });
const reset = () => Object.assign(new Error("lab"), { code: "ECONNRESET" });
const http = (status, retryAfter) => new rt.HttpError(status, { headers: retryAfter === undefined ? {} : { "retry-after": String(retryAfter) } });

/** A server: some workers and a queue, a timeline of trouble, seeded faults, a rate limit. What it was asked to do
 *  costs it time even if the client has stopped waiting. */
class Dependency {
  constructor(name, spec, sim) {
    Object.assign(this, { name, spec, sim, free: new Array(spec.workers ?? 0).fill(0), starts: [], hits: [], arrivals: [], effects: new Map() });
  }

  phase(now) {
    return (this.spec.phases ?? []).find((p) => p.from <= now && now < p.to) ?? {};
  }

  async attempt(call, n, key) {
    const { sim, spec } = this;
    const now = sim.now();
    this.arrivals.push(now);
    if (call.gaveUp) sim.wasted++;
    const who = [sim.seed, this.name, call.id, n];
    const p = this.phase(now);
    let mode = p.mode ?? null;
    if (mode === null && spec.fault_rate && u(...who, "p") < spec.fault_rate) mode = spec.kinds[Math.floor(u(...who, "k") * spec.kinds.length) % spec.kinds.length];
    if (mode === "refused") {
      await sleep(0.002);
      throw refused();
    }
    if (mode === "hang") {
      await sleep(spec.hang_s ?? 60);
      throw reset();
    }
    if (mode === "503") {
      await sleep(0.005);
      throw http(503);
    }
    if (spec.limit_rps) {
      this.hits = this.hits.filter((t) => Math.floor(t) === Math.floor(now));
      if (this.hits.length >= spec.limit_rps) {
        await sleep(0.002);
        throw http(429, 1); // until the next window, in whole seconds
      }
      this.hits.push(now);
    }
    let service = spec.service_s * (0.5 + u(...who, "s")) * (p.slower ?? 1);
    if (mode === "slow") service += spec.slow_s;
    let wait = 0;
    if (this.free.length) {
      this.starts = this.starts.filter((t) => t > now);
      if (this.starts.length >= spec.queue) {
        await sleep(0.005);
        throw http(503);
      }
      const first = this.free.indexOf(Math.min(...this.free));
      const start = Math.max(now, this.free[first]);
      this.free[first] = start + service;
      this.starts.push(start);
      wait = start - now;
    }
    if (call.op === "write") {
      if (!this.effects.has(call.id)) this.effects.set(call.id, new Set());
      this.effects.get(call.id).add(key ?? `attempt ${n}`);
    }
    if (mode === "lost") { // done, but the answer never arrives
      await sleep(spec.hang_s ?? 60);
      throw reset();
    }
    await sleep(wait + service);
    return { call: call.id };
  }
}

class Slots {
  constructor(n) {
    Object.assign(this, { n, waiting: [] });
  }

  async acquire() {
    if (this.n > 0) this.n--;
    else await new Promise((r) => this.waiting.push(r));
  }

  release() {
    const next = this.waiting.shift();
    if (next) next();
    else this.n++;
  }
}

class Sim {
  constructor(scenario, patterns, seed) {
    Object.assign(this, { sc: scenario, seed, on: patterns, t0: clock.t, wasted: 0, patience: scenario.patience_s, calls: [] });
    this.deps = Object.fromEntries(Object.entries(scenario.deps).map(([n, s]) => [n, new Dependency(n, s, this)]));
    this.policies = Object.fromEntries(Object.keys(this.deps).map((n) => {
      const cfg = { ...patterns };
      for (const [k, v] of Object.entries(scenario.params?.[n] ?? {})) if (cfg[k]) cfg[k] = { ...(typeof cfg[k] === "object" ? cfg[k] : {}), ...v };
      return [n, new rt.Policy(n, cfg)];
    }));
    this.slots = scenario.slots ? new Slots(scenario.slots) : null;
  }

  now() {
    return (clock.t - this.t0) / 1000;
  }

  ask(dep, call, stream) {
    const via = stream.via?.[dep];
    const write = call.op === "write";
    return this.policies[dep].call(async ({ attempt, idempotencyKey }) => {
      const out = await this.deps[dep].attempt(call, dep === stream.dep ? attempt : ++call.inner, idempotencyKey);
      if (via) await this.ask(via, call, stream);
      return out;
    }, { method: write && !this.sc.naive_writes ? "POST" : "GET", idempotencyKey: write ? rt.idempotencyKey("lab", call.id) : null,
      cacheKey: stream.keys ? `k${Math.floor(u(this.seed, "key", call.id) * stream.keys)}` : null });
  }

  async handle(call, stream) {
    await this.slots?.acquire();
    try {
      const left = call.at + this.patience - this.now();
      if (!this.on.deadline) return await this.ask(stream.dep, call, stream);
      return await rt.withDeadline(() => this.ask(stream.dep, call, stream), left * 1000); // the caller's deadline came with the request
    } finally {
      this.slots?.release();
    }
  }

  async one(call, stream) {
    let timer;
    const task = this.handle(call, stream).then((r) => ({ r }), (e) => ({ e }));
    const end = await Promise.race([task, new Promise((r) => { timer = setTimeout(r, this.patience * 1000, null); })]);
    clearTimeout(timer);
    call.latency = this.now() - call.at;
    if (end === null) { // the caller leaves; what it started goes on
      Object.assign(call, { gaveUp: true, outcome: "late" });
      await task;
    } else if (end.e) {
      call.outcome = "error";
      if (!(rt.isTransient(end.e) || end.e instanceof rt.RejectedError)) throw end.e;
    } else call.outcome = end.r === "degraded" ? "degraded" : end.r.call === call.id ? "fresh" : "stale";
  }

  async main() {
    const tasks = [];
    const feed = async (s, stream) => {
      let t = stream.from ?? 0;
      for (let i = 0; ;) {
        if (stream.burst !== undefined) {
          if (i >= stream.burst) return;
        } else t += -Math.log(1 - u(this.seed, "arrive", s, i)) / stream.rps;
        if (t >= (stream.to ?? this.sc.duration_s)) return;
        i++;
        if (t > this.now()) await sleep(t - this.now());
        const call = { id: `${s}.${i}`, stream: s, op: stream.op ?? "read", at: this.now(), gaveUp: false, inner: 0 };
        this.calls.push(call);
        tasks.push(this.one(call, stream));
      }
    };
    await Promise.all(this.sc.load.map((x, s) => feed(s, x)));
    await Promise.all(tasks);
  }

  result() {
    const r6 = (x) => Math.round(x * 1e6) / 1e6;
    let dups = 0;
    for (const d of Object.values(this.deps)) for (const k of d.effects.values()) dups += k.size - 1;
    return { calls: this.calls.map((c) => [c.stream, r6(c.at), c.outcome, r6(c.latency)]),
      arrivals: Object.fromEntries(Object.entries(this.deps).map(([n, d]) => [n, d.arrivals.map(r6)])), wasted: this.wasted, dups };
  }
}

async function runOne({ scenario, patterns, seed }) {
  clock.timers.clear();
  rt.resetState();
  rt.seed(seed);
  const sim = new Sim(scenario, patterns, seed);
  let over = null;
  sim.main().then(() => (over = {}), (e) => (over = { e }));
  for (;;) {
    await tick();
    if (over) break;
    if (!clock.advance()) {
      await tick();
      if (over) break;
      throw new Error("the simulation is stuck: nothing left to wait for");
    }
  }
  if (over.e) throw over.e;
  return sim.result();
}

async function main() {
  const job = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const write = process.stderr.write;
  const out = [];
  for (const run of job.runs) {
    process.stderr.write = () => true;
    try {
      out.push(await runOne(run));
    } finally {
      process.stderr.write = write;
    }
  }
  fs.writeFileSync(job.out, JSON.stringify(out));
  process.exit(0);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
