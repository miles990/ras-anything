// Fallback hierarchy: a fresh answer, else the last good one while it is young enough, else a declared degraded value,
// else the error. Never silent: every step down is logged.
"use strict";
const core = require("./core.cjs");

/** The last good answer per key, for at most ttlMs; at most maxEntries keys (the least recently stored go). Values are
 *  cloned in and out; what can't be cloned isn't kept. */
class StaleCache {
  #items = new Map();

  constructor({ ttlMs = 60_000, maxEntries = 1000 } = {}) {
    Object.assign(this, { ttlMs, maxEntries });
    core.track(this);
  }

  reset() {
    this.#items.clear();
  }

  put(key, value) {
    try {
      value = structuredClone(value);
    } catch {
      return;
    }
    this.#items.delete(key);
    this.#items.set(key, { at: core.nowMs(), value });
    while (this.#items.size > this.maxEntries) this.#items.delete(this.#items.keys().next().value);
  }

  /** {value, ageMs}, or null. */
  get(key) {
    const hit = this.#items.get(key);
    if (!hit) return null;
    const ageMs = core.nowMs() - hit.at;
    if (ageMs > this.ttlMs) {
      this.#items.delete(key);
      return null;
    }
    return { value: structuredClone(hit.value), ageMs };
  }
}

/** `degraded` is a value, or a function of the error; leave it null to end in the error. Only errors `when` accepts
 *  (default: transient ones) and this runtime's own rejections are stepped down for. */
class FallbackLayer extends core.Layer {
  constructor(dependency, params) {
    super(dependency, params);
    this.cache = new StaleCache({ ttlMs: params.staleTtlMs, maxEntries: params.maxEntries });
  }

  * run(call, inner) {
    const keyed = call.cacheKey !== null && call.cacheKey !== undefined && this.params.staleTtlMs > 0;
    let result;
    try {
      result = yield* inner(call);
    } catch (e) {
      const when = this.params.when ?? core.isTransient;
      if (!(e instanceof core.RejectedError) && !when(e)) throw e;
      const hit = keyed ? this.cache.get(call.cacheKey) : null;
      if (hit) {
        core.logEvent("fallback", { level: "stale", dependency: this.dependency, age_s: Math.round(hit.ageMs) / 1000, error: e });
        return hit.value;
      }
      const { degraded } = this.params;
      if (degraded === null || degraded === undefined) throw e;
      core.logEvent("fallback", { level: "degraded", dependency: this.dependency, error: e });
      return typeof degraded === "function" ? degraded(e) : degraded;
    }
    if (keyed) this.cache.put(call.cacheKey, result);
    return result;
  }
}

const PATTERN = { name: "fallback", order: 10, verdict: core.ADOPTED,
  params: { staleTtlMs: 60_000, maxEntries: 1000, degraded: null, when: null }, Layer: FallbackLayer };
module.exports = { PATTERN, StaleCache };
