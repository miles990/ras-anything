// Hedged requests: when an idempotent read is slow, send a second one and take whichever answers first.
"use strict";
const core = require("./core.cjs");

/** The q-quantile of the last `keep` successful attempts, once there were `least` of them. */
class LatencyTracker {
  #seen = [];

  constructor(quantile = 0.95, keep = 100, least = 20) {
    Object.assign(this, { quantile, keep, least });
    core.track(this);
  }

  reset() {
    this.#seen = [];
  }

  record(ms) {
    this.#seen.push(ms);
    if (this.#seen.length > this.keep) this.#seen.shift();
  }

  value() {
    const xs = [...this.#seen].sort((a, b) => a - b);
    return xs.length >= this.least ? xs[Math.min(xs.length - 1, Math.trunc(this.quantile * xs.length))] : null;
  }
}

const plan = (max, gate) => (started) => started <= max && gate();

/** fn({attempt, signal}); if it hasn't answered after delayMs, again (up to maxHedges more), and the first answer wins;
 *  the losers are aborted (pass `signal` on). Reads only. With a retryBudget each hedge also needs a token. */
async function hedge(fn, { delayMs, maxHedges = 1, method = "GET", retryBudget } = {}) {
  if (!core.SAFE_METHODS.has(method.toUpperCase())) throw new RangeError(`only idempotent reads may be hedged, not ${method}`);
  if (!(delayMs >= 0)) throw new RangeError("hedge needs delayMs");
  retryBudget?.recordRequest();
  function* steps() {
    return yield ["race", function* branch(i) {
      return yield ["call", { attempt: i + 1, timeoutMs: undefined, idempotencyKey: null, headers: {} }, null];
    }, delayMs, plan(maxHedges, () => !retryBudget || retryBudget.trySpend())];
  }
  return require("./policy.cjs").drive(steps(), fn);
}

/** After delayMs, or after the quantile of what this dependency's answers usually take, once known. */
class HedgeLayer extends core.Layer {
  constructor(dependency, params) {
    super(dependency, params);
    this.latency = new LatencyTracker(params.quantile);
  }

  * run(call, inner) {
    if (!call.safe || call.carryKey !== null) return yield* inner(call);
    const { latency } = this;
    function* branch() {
      const t0 = core.nowMs();
      const result = yield* inner(call);
      latency.record(core.nowMs() - t0);
      return result;
    }
    const known = latency.value();
    const delay = known === null ? this.params.delayMs : Math.max(this.params.minDelayMs, known);
    const gates = [...call.retryGates];
    return yield ["race", branch, delay, plan(this.params.maxHedges, () => gates.every((g) => g()))];
  }
}

const PATTERN = { name: "hedge", order: 110, verdict: core.OPT_IN,
  params: { delayMs: 1000, minDelayMs: 10, quantile: 0.95, maxHedges: 1 }, Layer: HedgeLayer };
module.exports = { PATTERN, hedge };
