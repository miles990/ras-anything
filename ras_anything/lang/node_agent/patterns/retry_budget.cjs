// Retry budget: a token bucket per dependency. Retries may only be a small share of the requests.
"use strict";
const core = require("./core.cjs");

/** Each request adds `ratio` tokens, each second `minPerS`, up to `burst`; each retry takes one. So over any stretch
 *  of time: retries <= burst + ratio x requests + minPerS x seconds. When the dependency is fine nobody notices; when
 *  it is down, retries stop multiplying the load. */
class RetryBudget {
  #tokens;
  #at;

  constructor({ ratio = 0.1, minPerS = 1, burst = 10 } = {}) {
    Object.assign(this, { ratio, minPerS, burst });
    this.reset();
    core.track(this);
  }

  reset() {
    this.#tokens = this.burst;
    this.#at = core.nowMs();
  }

  #refill() {
    const now = core.nowMs();
    this.#tokens = Math.min(this.burst, this.#tokens + this.minPerS * Math.max(0, now - this.#at) / 1000);
    this.#at = now;
  }

  get tokens() {
    this.#refill();
    return this.#tokens;
  }

  /** One logical call begins (not one attempt). */
  recordRequest() {
    this.#refill();
    this.#tokens = Math.min(this.burst, this.#tokens + this.ratio);
  }

  /** May one retry be made now? Takes its token if so. */
  trySpend() {
    this.#refill();
    if (this.#tokens < 1 - 1e-9) return false;
    this.#tokens -= 1;
    return true;
  }
}

class RetryBudgetLayer extends core.Layer {
  constructor(dependency, params) {
    super(dependency, params);
    this.budget = new RetryBudget(params);
  }

  * run(call, inner) {
    this.budget.recordRequest();
    call.retryGates.push(() => this.budget.trySpend()); // asked by retries and by hedges
    return yield* inner(call);
  }
}

const PATTERN = { name: "retry_budget", order: 50, verdict: core.ADOPTED, params: { ratio: 0.1, minPerS: 1, burst: 10 }, Layer: RetryBudgetLayer };
module.exports = { PATTERN, RetryBudget };
