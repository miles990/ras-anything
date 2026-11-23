// Attempt timeout: one attempt gets a limited time, cut to what is left of the deadline.
"use strict";
const core = require("./core.cjs");

/** The timeout for a boundary call made now: ms, or less when the deadline around this code ends sooner. Throws
 *  DeadlineExceeded when nothing is left; undefined only if there is no limit at all. */
function attemptTimeout(ms) {
  const left = core.remainingMs();
  if (left === null) return ms;
  if (left <= 0) throw new core.DeadlineExceeded();
  return ms === undefined ? left : Math.min(ms, left);
}

/** Sets attempt.timeoutMs; the async driver enforces it, synchronous code gives it to the boundary call. */
class AttemptTimeoutLayer extends core.Layer {
  * run(call, inner) {
    const before = call.timeoutMs;
    call.timeoutMs = this.params.timeoutMs;
    const left = call.deadline === null ? core.remainingMs() : call.deadline - core.nowMs();
    if (left !== null) {
      if (left <= 0) throw new core.DeadlineExceeded(`no time left for an attempt of ${this.dependency}`);
      call.timeoutMs = Math.min(call.timeoutMs, left);
    }
    try {
      return yield* inner(call);
    } finally {
      call.timeoutMs = before;
    }
  }
}

const PATTERN = { name: "attempt_timeout", order: 120, verdict: core.ADOPTED, params: { timeoutMs: 3500 }, Layer: AttemptTimeoutLayer };
module.exports = { PATTERN, attemptTimeout };
