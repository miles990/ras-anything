// Circuit breaker: stop calling a dependency that keeps failing; probe it before trusting it again.
"use strict";
const core = require("./core.cjs");

/** Thrown without calling the dependency while the breaker is open. */
class CircuitOpenError extends core.RejectedError {
  constructor(message, options) {
    super(message, options);
    this.name = "CircuitOpenError";
  }
}

/** Opens after failThreshold consecutive failures, or when at least failureRate of the last `window` calls failed (once
 *  there were minCalls; null switches this rule off). After cooldownMs it is half-open: up to halfOpenProbes calls at a
 *  time get through; that many successes close it, one failure re-opens it.
 *
 *  Only errors `count` accepts are failures; others (a 404, a bug) say nothing about the dependency. Outcomes of calls
 *  admitted before the breaker last changed state are ignored. */
class CircuitBreaker extends core.Guard {
  #gen = 0;
  #recent = [];
  #probes = 0;
  #probeSuccesses = 0;

  constructor({ failThreshold = 5, cooldownMs = 30_000, name = "dependency", count = core.dependencyFailed, failureRate = null, window = 50,
    minCalls = 20, halfOpenProbes = 1 } = {}) {
    super();
    if (!(halfOpenProbes >= 1 && window >= 1)) throw new RangeError("halfOpenProbes and window must be >= 1");
    Object.assign(this, { failThreshold, cooldownMs, name, count, failureRate, window, minCalls, halfOpenProbes });
    this.reset();
    core.track(this);
  }

  get state() {
    if (this.openedAt === null) return "closed";
    return core.nowMs() - this.openedAt >= this.cooldownMs ? "half_open" : "open";
  }

  reset() {
    this.failures = 0; // consecutive
    this.openedAt = null;
    this.#recent = [];
    this.#probes = this.#probeSuccesses = 0;
    this.#gen++;
  }

  /** Admit one call: a ticket for release(), or CircuitOpenError. */
  acquire() {
    const st = this.state;
    if (st === "open" || (st === "half_open" && this.#probes >= this.halfOpenProbes)) {
      throw new CircuitOpenError(`circuit open for ${this.name}`, { context: { failures: this.failures, cooldown_ms: this.cooldownMs } });
    }
    if (st === "half_open") this.#probes++;
    return { gen: this.#gen, probe: st === "half_open" };
  }

  /** Say how the admitted call ended. */
  release({ gen, probe }, error) {
    if (gen !== this.#gen) return;
    if (probe) this.#probes--;
    if (error === undefined) {
      this.failures = 0;
      if (!probe) this.#note(false);
      else if (++this.#probeSuccesses >= this.halfOpenProbes) this.reset();
    } else if (this.count(error)) {
      this.failures++;
      if (!probe) this.#note(true);
      if (probe || this.#tripped()) {
        const { failures } = this;
        this.reset();
        Object.assign(this, { failures, openedAt: core.nowMs() });
      }
    }
  }

  #note(failed) {
    this.#recent.push(failed);
    if (this.#recent.length > this.window) this.#recent.shift();
  }

  #tripped() {
    if (this.failThreshold !== null && this.failures >= this.failThreshold) return true;
    const n = this.#recent.length;
    return this.failureRate !== null && n >= this.minCalls && this.#recent.filter(Boolean).length >= this.failureRate * n;
  }
}

class CircuitBreakerLayer extends core.Layer {
  constructor(dependency, params) {
    super(dependency, params);
    this.breaker = new CircuitBreaker({ ...params, name: dependency });
  }

  run(call, inner) {
    return this.breaker.around(call, inner);
  }
}

const PATTERN = { name: "circuit_breaker", order: 80, verdict: core.ADOPTED,
  params: { failThreshold: 5, cooldownMs: 5000, failureRate: 0.5, window: 50, minCalls: 20, halfOpenProbes: 1 }, Layer: CircuitBreakerLayer };
module.exports = { PATTERN, CircuitBreaker, CircuitOpenError };
