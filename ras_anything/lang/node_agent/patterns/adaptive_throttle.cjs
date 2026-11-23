// Adaptive throttling (Google SRE): a client that sees rejections starts rejecting its own requests.
"use strict";
const core = require("./core.cjs");

/** Thrown without calling the dependency: shed locally because it is rejecting too much. */
class ThrottledError extends core.RejectedError {
  constructor(message, options) {
    super(message, options);
    this.name = "ThrottledError";
  }
}

/** Rejects a request with probability max(0, (requests - k * accepts) / (requests + 1)), both counted over the last
 *  windowMs. `requests` counts every attempt asked for, also the ones rejected here, once its outcome is known (a burst
 *  still in flight is no rejection); `accepts` the ones the dependency took (`count` says which errors mean it didn't). */
class AdaptiveThrottle extends core.Guard {
  #buckets = []; // [bucket number, requests, accepts]

  constructor({ k = 2, windowMs = 120_000, name = "dependency", count = core.notAccepted } = {}) {
    super();
    Object.assign(this, { k, windowMs, name, count });
    this.reset();
    core.track(this);
  }

  reset() {
    this.#buckets = [];
    this.requests = this.accepts = 0;
  }

  #bucket() {
    const now = Math.floor(core.nowMs() / (this.windowMs / 24));
    while (this.#buckets.length && this.#buckets[0][0] <= now - 24) {
      const [, r, a] = this.#buckets.shift();
      this.requests -= r;
      this.accepts -= a;
    }
    if (this.#buckets.at(-1)?.[0] !== now) this.#buckets.push([now, 0, 0]);
    return this.#buckets.at(-1);
  }

  rejectProbability() {
    this.#bucket();
    return Math.max(0, (this.requests - this.k * this.accepts) / (this.requests + 1));
  }

  /** Ask for one attempt; ThrottledError when it is shed. */
  acquire() {
    const p = this.rejectProbability();
    if (p > 0 && core.random() < p) {
      this.#count(0);
      throw new ThrottledError(`shedding load to ${this.name}`, { dependency: this.name, context: { reject_probability: Math.round(p * 1000) / 1000 } });
    }
  }

  /** Say how the attempt ended. */
  release(_ticket, error) {
    this.#count(error === undefined || !this.count(error) ? 1 : 0);
  }

  #count(accepted) {
    const b = this.#bucket();
    b[1]++;
    b[2] += accepted;
    this.requests++;
    this.accepts += accepted;
  }
}

class AdaptiveThrottleLayer extends core.Layer {
  constructor(dependency, params) {
    super(dependency, params);
    this.throttle = new AdaptiveThrottle({ ...params, name: dependency });
  }

  run(call, inner) {
    return this.throttle.around(call, inner);
  }
}

const PATTERN = { name: "adaptive_throttle", order: 90, verdict: core.OPT_IN, params: { k: 2, windowMs: 120_000 }, Layer: AdaptiveThrottleLayer };
module.exports = { PATTERN, AdaptiveThrottle, ThrottledError };
