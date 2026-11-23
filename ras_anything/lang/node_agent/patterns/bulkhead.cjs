// Bulkhead: at most so many calls at a time into one dependency; when full, fail at once.
"use strict";
const core = require("./core.cjs");

/** Thrown without calling the dependency: its compartment is full. */
class BulkheadFullError extends core.RejectedError {
  constructor(message, options) {
    super(message, options);
    this.name = "BulkheadFullError";
  }
}

/** A semaphore per dependency that never waits: a slow dependency can't take every connection or task of the app
 *  with it. */
class Bulkhead extends core.Guard {
  constructor({ maxConcurrent = 10, name = "dependency" } = {}) {
    super();
    if (!(maxConcurrent >= 1)) throw new RangeError("maxConcurrent must be >= 1");
    Object.assign(this, { maxConcurrent, name, inUse: 0 });
    core.track(this);
  }

  reset() {} // calls in progress stay in progress

  acquire() {
    if (this.inUse >= this.maxConcurrent) throw new BulkheadFullError(`bulkhead full for ${this.name}`, { context: { max_concurrent: this.maxConcurrent } });
    this.inUse++;
  }

  release() {
    this.inUse--;
  }
}

class BulkheadLayer extends core.Layer {
  constructor(dependency, params) {
    super(dependency, params);
    this.bulkhead = new Bulkhead({ ...params, name: dependency });
  }

  run(call, inner) {
    return this.bulkhead.around(call, inner);
  }
}

const PATTERN = { name: "bulkhead", order: 30, verdict: core.OPT_IN, params: { maxConcurrent: 10 }, Layer: BulkheadLayer };
module.exports = { PATTERN, Bulkhead, BulkheadFullError };
