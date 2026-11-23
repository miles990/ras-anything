// Policy: the patterns composed around one dependency, in a fixed order, async or sync.
//
//   const weather = new Policy("weather-api", { deadline: { timeoutMs: 4800 }, attemptTimeout: { timeoutMs: 3500 } });
//   const forecast = (url) => weather.call(async ({ signal }) => (await raiseForStatus(await fetch(url, { signal }))).json());
//
// Adopted patterns are on, the others off; an option named after a pattern switches it (true/false) or sets its
// parameters (an object). Order, outermost first: fallback, deadline, bulkhead, idempotency, retry budget, Retry-After,
// retry, circuit breaker, adaptive throttle, adaptive concurrency, hedge, attempt timeout. Create a policy once per
// dependency (module level): its state is what it has learned about it.
"use strict";
const core = require("./core.cjs");
const registry = require("./index.cjs");

class Policy {
  constructor(dependency, patterns = {}) {
    this.dependency = dependency;
    this.layers = [];
    const known = new Map(registry.all().map((p) => [registry.camel(p.name), p]));
    const unknown = Object.keys(patterns).filter((k) => !known.has(k));
    if (unknown.length) throw new TypeError(`unknown pattern ${unknown.join(", ")} (known: ${[...known.keys()].join(", ")})`);
    for (const [key, p] of known) {
      const given = patterns[key];
      const on = given === undefined || given === null ? p.verdict === core.ADOPTED : Boolean(given);
      const params = { ...p.params };
      if (typeof given === "object" && given !== null) {
        const bad = Object.keys(given).filter((k) => !(k in params));
        if (bad.length) throw new TypeError(`${key} has no parameter ${bad.join(", ")} (it has: ${Object.keys(params).join(", ") || "none"})`);
        Object.assign(params, given);
      }
      this.layers.push({ name: p.name, on, layer: new p.Layer(dependency, params) });
    }
  }

  /** The layer of a pattern (its state: breaker, budget, ...), switched on or not. */
  layer(name) {
    return this.layers.find((x) => x.name === registry.snake(name)).layer;
  }

  steps(options) {
    const call = new core.Call(this.dependency, options);
    const on = this.layers.filter((x) => registry.hooks.isOn(this.dependency, x.name, x.on)).map((x) => x.layer);
    function* attempt(c) {
      let t = c.timeoutMs ?? undefined;
      if (t === undefined && c.deadline !== null) t = Math.max(1, c.deadline - core.nowMs()); // no attempt timeout: what is left of the whole
      const a = { attempt: ++c.attempts, timeoutMs: t, idempotencyKey: c.carryKey, headers: { ...c.headers } };
      return yield ["call", a, ends(c)];
    }
    const chain = (i) => (i === on.length ? attempt : (c) => on[i].run(c, chain(i + 1)));
    return chain(0)(call);
  }

  /** fn({attempt, signal, timeoutMs, idempotencyKey, headers}) under this policy; attempt timeouts are enforced (pass
   *  `signal` on). `method` other than GET/HEAD/OPTIONS/RUN is a write: it is only repeated when idempotencyKey is
   *  given (send `headers` with every attempt). */
  call(fn, options = {}) {
    return drive(this.steps(options), fn);
  }

  /** call() for synchronous code (execFileSync, ...): give timeoutMs to the call. No hedging. */
  callSync(fn, options = {}) {
    return driveSync(this.steps(options), fn);
  }
}

/** The deadline the attempt's own nested calls must respect (null when deadlines are off). */
function ends(c) {
  if (c.deadline === null) return null;
  return c.timeoutMs === null ? c.deadline : Math.min(c.deadline, core.nowMs() + c.timeoutMs);
}

// ---------------------------------------------------------------- drivers

/** Runs a layer generator. `stop` aborts its attempts (the loser of a race). */
async function drive(steps, fn, stop) {
  let r = steps.next();
  while (!r.done) {
    let value;
    try {
      const [op, x, y, z] = r.value;
      if (op === "call") value = await core.runUntil(y, () => attemptOnce(x, fn, stop));
      else if (op === "sleep") await core.sleep(x);
      else value = await race(x, y, z, fn);
      if (stop?.aborted) throw stop.reason;
    } catch (e) {
      r = steps.throw(e);
      continue;
    }
    r = steps.next(value);
  }
  return r.value;
}

function attemptOnce(a, fn, stop) {
  if (a.timeoutMs === undefined) return fn({ ...a, signal: stop });
  return core.withTimeout((signal) => fn({ ...a, signal }), a.timeoutMs, { signal: stop });
}

function driveSync(steps, fn) {
  let r = steps.next();
  while (!r.done) {
    let value;
    try {
      const [op, x, y] = r.value;
      if (op === "call") value = core.runUntil(y, () => fn({ ...x, timeoutMs: x.timeoutMs === undefined ? undefined : Math.max(1, Math.ceil(x.timeoutMs)) }));
      else if (op === "sleep") core.sleepSync(x);
      else value = driveSync(x(0), fn); // nothing runs concurrently here: the first branch only
    } catch (e) {
      r = steps.throw(e);
      continue;
    }
    r = steps.next(value);
  }
  return r.value;
}

/** Branch 0 now; each further one after delayMs without an answer, while gate() allows it. First success wins; the
 *  losers are aborted. */
function race(make, delayMs, gate, fn) {
  return new Promise((resolve, reject) => {
    const stops = [];
    let pending = 0;
    let first = null;
    let timer = null;
    const end = (settle, v) => {
      clearTimeout(timer);
      settle(v);
      for (const s of stops) s.abort(new DOMException("another attempt answered first", "AbortError"));
    };
    const start = () => {
      const stop = new AbortController();
      stops.push(stop);
      pending++;
      drive(make(stops.length - 1), fn, stop.signal).then((v) => end(resolve, v), (e) => {
        pending--;
        first ??= { e };
        clearTimeout(timer); // it failed: no (more) hedges, the others decide
        if (pending === 0) end(reject, first.e);
      });
    };
    const arm = () => {
      timer = setTimeout(() => {
        if (first || !gate(stops.length)) return;
        start();
        arm();
      }, delayMs);
    };
    start();
    arm();
  });
}

module.exports = { Policy, drive, driveSync };
