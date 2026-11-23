// Adaptive concurrency: an AIMD limit on calls in flight (like TCP): +1 per round of successes, x backoff on failure.
"use strict";
const core = require("./core.cjs");

/** Thrown without calling the dependency: the current concurrency limit is reached. */
class ConcurrencyLimitError extends core.RejectedError {
  constructor(message, options) {
    super(message, options);
    this.name = "ConcurrencyLimitError";
  }
}

/** Admits while inFlight < limit. A success while the limit is in use adds 1/limit, an error `count` accepts multiplies
 *  the limit by `backoff` (at most once per limit's worth of calls), within [minLimit, maxLimit]. */
class AdaptiveConcurrency extends core.Guard {
  #ended = 0;
  #cutAt = 0; // calls ended when the limit was last cut

  constructor({ initial = 20, minLimit = 1, maxLimit = 200, backoff = 0.7, name = "dependency", count = core.notAccepted } = {}) {
    super();
    Object.assign(this, { initial, minLimit, maxLimit, backoff, name, count, inFlight: 0 });
    this.reset();
    core.track(this);
  }

  reset() {
    this.limit = this.initial;
    this.#ended = this.#cutAt = 0;
  }

  acquire() {
    if (this.inFlight >= Math.trunc(this.limit)) {
      throw new ConcurrencyLimitError(`concurrency limit for ${this.name}`, { dependency: this.name, context: { limit: Math.trunc(this.limit) } });
    }
    this.inFlight++;
  }

  release(_ticket, error) {
    const used = this.inFlight--;
    this.#ended++;
    if (error !== undefined && this.count(error)) {
      if (this.#ended - this.#cutAt >= this.limit || this.#cutAt === 0) {
        this.limit = Math.max(this.minLimit, this.limit * this.backoff);
        this.#cutAt = this.#ended;
      }
    } else if (error === undefined && used * 2 >= this.limit) {
      this.limit = Math.min(this.maxLimit, this.limit + 1 / this.limit);
    }
  }
}

class AdaptiveConcurrencyLayer extends core.Layer {
  constructor(dependency, params) {
    super(dependency, params);
    this.limiter = new AdaptiveConcurrency({ ...params, name: dependency });
  }

  run(call, inner) {
    return this.limiter.around(call, inner);
  }
}

const PATTERN = { name: "adaptive_concurrency", order: 100, verdict: core.OPT_IN,
  params: { initial: 20, minLimit: 1, maxLimit: 200, backoff: 0.7 }, Layer: AdaptiveConcurrencyLayer };
module.exports = { PATTERN, AdaptiveConcurrency, ConcurrencyLimitError };
