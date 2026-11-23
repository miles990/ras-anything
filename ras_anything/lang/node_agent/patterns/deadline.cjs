// Total deadline: the whole call, waits included, ends in time, and so does everything it calls.
"use strict";
const core = require("./core.cjs");

/** call.deadline = now + timeoutMs, or the caller's own deadline if that is earlier. The user's function runs with the
 *  deadline in its async context, so nested policies, retry() and withTimeout() see it. */
class DeadlineLayer extends core.Layer {
  * run(call, inner) {
    const now = core.nowMs();
    const left = core.remainingMs();
    if (left !== null && left <= 0) throw new core.DeadlineExceeded(`the caller's deadline has passed: not calling ${this.dependency}`);
    const end = now + this.params.timeoutMs;
    call.deadline = left === null ? end : Math.min(end, now + left);
    return yield* inner(call);
  }
}

const PATTERN = { name: "deadline", order: 20, verdict: core.ADOPTED, params: { timeoutMs: 4800 }, Layer: DeadlineLayer };
module.exports = { PATTERN, withDeadline: core.withDeadline, remainingMs: core.remainingMs, DeadlineExceeded: core.DeadlineExceeded };
