// The fault plans and clocks of ras_anything/faults.py, for the Node agent: same plans, same draws.
"use strict";
const { AsyncResource } = require("node:async_hooks");
const { createHash } = require("node:crypto");
const real = {
  setTimeout, clearTimeout, setInterval, clearInterval, setImmediate, Date, AbortSignalTimeout: AbortSignal.timeout,
  AbortSignalAny: AbortSignal.any, now: performance.now.bind(performance), hrtime: process.hrtime, wait: Atomics.wait,
  timers: require("node:timers"), timersPromises: require("node:timers/promises"), random: Math.random,
};

const KINDS = ["timeout", "refused", "http5xx", "http429", "slow", "truncated"];
const C = { slow_s: 3, hang_cap_s: 60, outage_k: 3, retry_after: "1" };

/** The first 8 bytes of sha256(parts joined with "|") as a number in [0, 1). */
function u(...parts) {
  const h = createHash("sha256").update(parts.join("|")).digest();
  return Number(h.readBigUInt64BE(0)) / 2 ** 64;
}

/** fault(key, n) for a plan in the wire format; its .outage(key, n) says whether that attempt is inside an outage window. */
function plan(p) {
  const fault = faultOf(p);
  fault.outage = (key, n) => {
    if (p?.type === "down") return true;
    if (!(n < C.outage_k)) return false;
    if (p?.type === "first") return p.fault === "outage" && [null, undefined, key].includes(p.key);
    return p?.type === "seeded" && p.outage_rate > 0 && u(p.seed, p.salt ?? "", key, "outage") < p.outage_rate;
  };
  return fault;
}

function faultOf(p) {
  if (!p || p.type === "none") return () => null;
  if (p.type === "down") return () => "refused";
  if (p.type === "first") {
    return (key, n) => {
      if (p.key !== null && p.key !== undefined && key !== p.key) return null;
      if (p.fault === "outage") return n < C.outage_k ? "refused" : null;
      return n === 0 ? p.fault : null;
    };
  }
  if (p.type === "schedule") {
    const list = Array.isArray(p.faults) ? p.faults : [];
    const rep = p.repeat !== false;
    return (key, n) => {
      if (!list.length || (p.key !== null && p.key !== undefined && key !== p.key)) return null;
      return n < list.length ? list[n] : rep ? list.at(-1) : null;
    };
  }
  if (p.type === "seeded") {
    const { seed, rate, outage_rate: outage = 0, salt = "" } = p;
    return (key, n) => {
      if (outage && n < C.outage_k && u(seed, salt, key, "outage") < outage) return "refused";
      if (u(seed, salt, key, n, "p") < rate) return KINDS[Math.floor(u(seed, salt, key, n, "k") * KINDS.length) % KINDS.length];
      return null;
    };
  }
  throw new Error(`unknown plan ${p.type}`);
}

/** Raised out of a synchronous call that would have blocked forever. It is reported even if it is caught. */
class ChaosHang extends Error {
  constructor(msg = "boundary call with no timeout hung (chaos)") {
    super(msg);
    this.name = "ChaosHang";
  }
}

// ---------------------------------------------------------------- live clock

/** Real time with injected waits scaled down; nothing waits longer than hangS. A call that waits for an
 *  answer that never comes is declared a hang (onHang) once the app has nothing else left to do. */
class LiveClock {
  constructor(scale = 0.05, hangS = 2, onHang = () => {}) {
    Object.assign(this, { virtual: false, scale, hangS, onHang, activity: real.now(), hung: new Set() });
  }

  touch() {
    this.activity = real.now();
  }

  scaled(s) {
    return Math.min(s * this.scale, this.hangS) * 1000;
  }

  /** Waits s seconds or until ctl is cancelled (-> true). */
  async wait(s, ctl) {
    const t = ctl.timeoutS;
    if (t !== null && t <= s) { // the caller's own timeout comes first: bring it forward
      await delay(this.scaled(t), ctl);
      if (!ctl.cancelled) ctl.fire();
      if (ctl.cancelled) return true;
    }
    if (s === Infinity) await this.hang(ctl);
    else await delay(this.scaled(s), ctl);
    return ctl.cancelled;
  }

  waitSync(s) {
    if (s > 0) real.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, this.scaled(s));
  }

  hangSync(where) {
    this.waitSync(this.hangS / this.scale);
    this.onHang(where);
  }

  /** Pending until cancelled; meanwhile watches for the whole app being stuck. */
  hang(ctl) {
    return new Promise((resolve) => {
      const me = { since: real.now(), timer: null };
      this.hung.add(me);
      const check = () => {
        const quiet = real.now() - Math.max(this.activity, me.since) >= this.hangS * 1000;
        if (quiet && !this.othersPending()) return this.onHang(ctl.where);
        me.timer = real.setTimeout(check, 100);
      };
      me.timer = real.setTimeout(check, this.hangS * 1000);
      ctl.onCancel(() => {
        this.hung.delete(me);
        real.clearTimeout(me.timer);
        resolve();
      });
    });
  }

  /** Is something other than hung calls still going to happen (a timer, a child process, file I/O)? */
  othersPending() {
    const kinds = process.getActiveResourcesInfo();
    const timers = kinds.filter((k) => k === "Timeout").length - this.hung.size; // each hung call has one of ours
    return timers > 0 || kinds.some((k) => k === "Immediate" || k === "ProcessWrap" || k.startsWith("FSReq"));
  }
}

function delay(ms, ctl) {
  return new Promise((resolve) => {
    const t = real.setTimeout(resolve, ms);
    ctl.onCancel(() => {
      real.clearTimeout(t);
      resolve();
    });
  });
}

// ---------------------------------------------------------------- virtual clock

class VTimeout {
  constructor(clock, fn, ms, args, repeat) {
    Object.assign(this, { clock, fn: AsyncResource.bind(fn), args, repeat }); // (a callback runs in the context that set it)
    this.ms = !(ms >= 1 && ms <= 2 ** 31 - 1) ? 1 : Math.trunc(ms);
    this.refresh();
  }

  refresh() {
    this.at = this.clock.t + this.ms;
    this.seq = this.clock.seq++;
    this.clock.timers.add(this);
    return this;
  }

  close() {
    this.clock.timers.delete(this);
    return this;
  }

  ref() { return this; }
  unref() { return this; }
  hasRef() { return true; }
  [Symbol.toPrimitive]() { return this.seq + 1; }
  [Symbol.dispose]() { this.close(); }
}

/** Simulated time in milliseconds: timers, Date, performance.now, hrtime, AbortSignal.timeout and
 *  Atomics.wait all follow it. The replay driver moves it on when nothing else can happen. */
class VirtualClock {
  constructor() {
    Object.assign(this, { virtual: true, t: 1e9, start: 1e9, seq: 0, timers: new Set(), hangs: 0 });
    this.wall = real.Date.now() - this.t;
    this.limitS = C.hang_cap_s * 20;
  }

  touch() {}

  reset() {
    this.timers.clear();
    this.start = this.t;
    this.hangs = 0;
  }

  elapsed() {
    return (this.t - this.start) / 1000;
  }

  /** Fires the next timer (moving the clock to it); false when there is none. */
  advance(limit = Infinity) {
    let next = null;
    for (const x of this.timers) if (!next || x.at < next.at || (x.at === next.at && x.seq < next.seq)) next = x;
    if (!next || next.at > limit) return false;
    this.t = Math.max(this.t, next.at);
    if (next.repeat) next.refresh();
    else this.timers.delete(next);
    next.fn(...next.args);
    return true;
  }

  async wait(s, ctl) {
    if (s === Infinity) await new Promise((r) => ctl.onCancel(r));
    else if (s > 0) {
      await new Promise((resolve) => {
        const t = new VTimeout(this, resolve, 1, [], false);
        t.at = this.t + s * 1000;
        ctl.onCancel(() => {
          t.close();
          resolve();
        });
      });
    }
    return ctl.cancelled;
  }

  waitSync(s) {
    if (s > 0) this.t += s * 1000;
    if (this.elapsed() > this.limitS) {
      this.hangs++;
      throw new ChaosHang("call kept running for too long");
    }
  }

  hangSync() {
    this.hangs++;
    this.t += C.hang_cap_s * 1000;
    throw new ChaosHang();
  }

  install() {
    const clock = this;
    const set = (fn, ms, ...args) => {
      if (typeof fn !== "function") throw new TypeError('The "callback" argument must be of type function');
      return new VTimeout(clock, fn, ms, args, false);
    };
    const every = (fn, ms, ...args) => new VTimeout(clock, fn, ms, args, true);
    const clear = (t) => {
      if (t instanceof VTimeout) t.close();
      else if (typeof t === "number") for (const x of clock.timers) if (x.seq + 1 === t) x.close();
    };
    const sleep = (ms, value, { signal } = {}) => new Promise((resolve, reject) => {
      const abort = () => {
        t.close();
        reject(abortError(signal));
      };
      const t = new VTimeout(clock, () => {
        signal?.removeEventListener("abort", abort);
        resolve(value);
      }, ms, [], false);
      if (signal?.aborted) abort();
      else signal?.addEventListener("abort", abort, { once: true });
    });
    set[require("node:util").promisify.custom] = sleep;
    Object.assign(globalThis, { setTimeout: set, setInterval: every, clearTimeout: clear, clearInterval: clear });
    Object.assign(real.timers, { setTimeout: set, setInterval: every, clearTimeout: clear, clearInterval: clear });
    async function* ticks(ms, value, opts = {}) {
      for (;;) {
        await sleep(ms, undefined, opts);
        yield value;
      }
    }
    Object.assign(real.timersPromises, { setTimeout: sleep, setInterval: ticks });
    if (real.timersPromises.scheduler) real.timersPromises.scheduler.wait = (ms, opts) => sleep(ms, undefined, opts);
    const now = () => clock.t + clock.wall;
    globalThis.Date = new Proxy(real.Date, {
      construct: (D, args, nt) => Reflect.construct(D, args.length ? args : [now()], nt === globalThis.Date ? D : nt),
      apply: (D) => new D(now()).toString(),
      get: (D, k) => (k === "now" ? now : D[k]),
    });
    performance.now = () => clock.t;
    process.hrtime = Object.assign((prev) => {
      const ns = BigInt(Math.round(clock.t * 1e6));
      let s = Number(ns / 1_000_000_000n);
      let n = Number(ns % 1_000_000_000n);
      if (prev) {
        s -= prev[0];
        n -= prev[1];
        if (n < 0) {
          s--;
          n += 1e9;
        }
      }
      return [s, n];
    }, { bigint: () => BigInt(Math.round(clock.t * 1e6)) });
    Atomics.wait = (arr, i, v, ms = Infinity) => {
      if (Atomics.load(arr, i) !== v) return "not-equal";
      if (!Number.isFinite(ms)) clock.hangSync();
      clock.waitSync(ms / 1000);
      return "timed-out";
    };
    patchAbortSignal((ms, fire) => set(fire, ms));
    require("node:module").syncBuiltinESMExports();
  }
}

function abortError(signal) {
  const e = new Error("The operation was aborted", { cause: signal?.reason });
  e.name = "AbortError";
  e.code = "ABORT_ERR";
  return e;
}

// ---------------------------------------------------------------- AbortSignal.timeout

const timeouts = new WeakMap(); // signal -> {s, fire}: signals made by AbortSignal.timeout (or any() of them)

/** AbortSignal.timeout on our timers, remembering each signal's seconds so a fault can bring it forward. */
function patchAbortSignal(schedule) {
  AbortSignal.timeout = function timeout(ms) {
    if (typeof ms !== "number" || !(ms >= 0)) return real.AbortSignalTimeout.call(AbortSignal, ms); // its own error
    const c = new AbortController();
    const fire = () => c.abort(new DOMException("The operation was aborted due to timeout", "TimeoutError"));
    schedule(ms, fire);
    timeouts.set(c.signal, { s: ms / 1000, fire });
    return c.signal;
  };
  AbortSignal.any = function any(signals) {
    const list = [...signals];
    const s = real.AbortSignalAny.call(AbortSignal, list);
    const known = list.map((x) => timeouts.get(x)).filter(Boolean).sort((a, b) => a.s - b.s)[0];
    if (known) timeouts.set(s, known);
    return s;
  };
}

const timeoutOf = (signal) => (signal ? timeouts.get(signal) ?? null : null);

module.exports = { C, KINDS, u, plan, ChaosHang, LiveClock, VirtualClock, patchAbortSignal, timeoutOf, real, abortError };
