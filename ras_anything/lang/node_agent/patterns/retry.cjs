// Retry: exponential backoff with full jitter, a limited number of attempts, transient errors only.
"use strict";
const core = require("./core.cjs");

/** The Retry-After delay (seconds) an HTTP error or Response asked for, if any. */
const retryAfter = (err) => {
  try {
    return require("./retry_after.cjs").retryAfterS(err);
  } catch {
    return null;
  }
};

/** Milliseconds to wait after the n-th failure (n = 1, 2, ...). */
function jitter(n, baseMs, capMs, full = true) {
  const top = Math.min(capMs, baseMs * 2 ** (n - 1));
  return full ? core.random() * top : top;
}

function stepper({ attempts = 3, baseMs = 100, capMs = 2000, budgetMs, retryOn = core.isTransient, onRetry, retryBudget }) {
  if (!(attempts >= 1)) throw new RangeError("attempts must be >= 1");
  const start = core.nowMs();
  const ambient = core.remainingMs();
  const end = Math.min(budgetMs === undefined ? Infinity : start + budgetMs, ambient === null ? Infinity : start + ambient);
  let n = 0;
  retryBudget?.recordRequest();
  return {
    end: Number.isFinite(end) ? end : null,
    left: () => end - core.nowMs(),
    /** ms to wait before the next attempt, or null to give up. */
    next(err) {
      n++;
      if (n >= attempts || !retryOn(err)) return null;
      let delay = jitter(n, baseMs, capMs);
      const ra = retryAfter(err);
      if (ra !== null) delay = Math.max(delay, ra * 1000);
      if (core.nowMs() + delay >= end) return null;
      if (retryBudget && !retryBudget.trySpend()) return null;
      onRetry?.(n, err, delay);
      return delay;
    },
  };
}

/** Calls fn({attempt, signal}) up to `attempts` times, retrying only errors `retryOn` accepts.
 *
 *  Backoff is full jitter, at least the server's Retry-After. timeoutMs bounds each attempt (pass `signal` on),
 *  budgetMs the whole, and so does a withDeadline() around it: attempts are cut to what is left and no wait crosses it.
 *  With a retryBudget (RetryBudget, shared by all calls of a dependency) a retry also needs a token. The last error is
 *  rethrown as is. */
async function retry(fn, options = {}) {
  const { timeoutMs } = options;
  const step = stepper(options);
  for (let attempt = 1; ; attempt++) {
    try {
      const limit = Math.min(timeoutMs ?? Infinity, Math.max(1, step.left()));
      return await core.runUntil(step.end, () => (Number.isFinite(limit) ? core.withTimeout((signal) => fn({ attempt, signal }), limit)
        : fn({ attempt, signal: undefined })));
    } catch (e) {
      const delay = step.next(e);
      if (delay === null) throw e;
      await core.sleep(delay);
    }
  }
}

/** retry() for synchronous code (execFileSync, ...): fn({attempt, timeoutMs}); give timeoutMs to the call. */
function retrySync(fn, options = {}) {
  const step = stepper(options);
  for (let attempt = 1; ; attempt++) {
    try {
      const limit = Math.min(options.timeoutMs ?? Infinity, Math.max(1, step.left()));
      return core.runUntil(step.end, () => fn({ attempt, timeoutMs: Number.isFinite(limit) ? Math.ceil(limit) : undefined }));
    } catch (e) {
      const delay = step.next(e);
      if (delay === null) throw e;
      core.sleepSync(delay);
    }
  }
}

class RetryLayer extends core.Layer {
  * run(call, inner) {
    const p = this.params;
    const retryOn = p.retryOn ?? core.isTransient;
    for (let n = 1; ; n++) {
      let delay;
      try {
        return yield* inner(call);
      } catch (e) {
        if (n >= p.attempts || !call.repeatable || !(retryOn(e) || call.retryRules.some((r) => r(e)))) throw e;
        delay = jitter(n, p.baseMs, p.capMs, p.fullJitter);
        for (const rule of call.delayRules) delay = rule(e, delay);
        if (call.deadline !== null && core.nowMs() + delay >= call.deadline) throw e;
        if (!call.retryGates.every((gate) => gate())) throw e;
      }
      yield ["sleep", delay];
    }
  }
}

const PATTERN = { name: "retry", order: 70, verdict: core.ADOPTED,
  params: { attempts: 3, baseMs: 100, capMs: 2000, fullJitter: true, retryOn: null }, Layer: RetryLayer };
module.exports = { PATTERN, retry, retrySync };
