// Reliability helpers for hardened code. Node's built-ins only.
//
// Hardened files import these from "./_runtime.cjs" (ras-anything enable puts this file next to them):
//
//   import { retry, withTimeout, raiseForStatus, RasError, logEvent } from "./_runtime.cjs";
//
// Timers, Date and AbortSignal.timeout are looked up at call time, so `ras-anything verify` can run your code
// on a virtual clock. Each resilience pattern is a module of its own in patterns/ (docs/PATTERNS.md); the core they share
// is patterns/core.cjs. Randomness (jitter, trace ids) comes from one seedable generator: seed(n).
"use strict";
const core = require("./patterns/core.cjs");
const registry = require("./patterns/index.cjs");
const { Policy } = require("./patterns/policy.cjs");

const { RasError, isTransient, logEvent } = core;

// ---------------------------------------------------------------- HTTP

/** An HTTP answer that isn't a success: status, headers and the first part of the body. */
class HttpError extends Error {
  constructor(status, { url = "", headers = {}, body = "" } = {}) {
    super(`HTTP ${status}${url ? ` from ${url}` : ""}`);
    this.name = "HttpError";
    Object.assign(this, { status, url, headers, body });
  }
}

/** The fetch Response itself if it is a 2xx/3xx, else an HttpError (isTransient and retry understand it). */
async function raiseForStatus(res) {
  if (res.status < 400) return res;
  let body = "";
  try {
    body = (await res.text()).slice(0, 500);
  } catch {}
  throw new HttpError(res.status, { url: res.url, headers: Object.fromEntries(res.headers), body });
}

// ---------------------------------------------------------------- fallback

/** fn()'s result; when it fails with an error `when` accepts, logs the give-up and returns the fallback
 *  (a value, or a function of the error). Other errors are rethrown. */
async function withFallback(fn, fallback, { when = isTransient, event = "fallback", fields = {} } = {}) {
  try {
    return await fn();
  } catch (e) {
    if (!when(e)) throw e;
    logEvent(event, { ...fields, error: e });
    return typeof fallback === "function" ? fallback(e) : fallback;
  }
}

// ---------------------------------------------------------------- enable

const scopes = new Map();

/** Live module-level names of the function's own module: `const { NAME } = scope("<id>")`. */
function scope(id) {
  const s = scopes.get(id);
  if (!s) throw new RasError(`no scope for ${id}: hardened code is loaded by the block ras-anything enable adds`);
  return new Proxy(s, {
    get(o, k) {
      if (typeof k === "string" && !(k in o)) throw new RasError(`scope("${id}") has no ${k}: only the module-level names the original uses`);
      return o[k];
    },
  });
}

/** Used by the marked block: the function that runs hardened code, loaded on the first call; if it can't be
 *  loaded, the original, loudly. */
function enable({ id, name, original, names, load, module }) {
  if (names) scopes.set(id, names);
  let impl = null;
  const swaps = globalThis[Symbol.for("ras-anything.swaps")];
  const get = () => {
    if (impl) return impl;
    try {
      const mod = load();
      impl = mod[name];
      if (typeof impl !== "function") throw new TypeError(`it exports no function ${name}`);
      swaps?.set(id, { module: mod });
    } catch (e) { // keep the app running on the original
      impl = original;
      process.stderr.write(`ras-anything: hardened ${id} not loaded, running the original: ${e?.stack ?? e}\n`);
    }
    return impl;
  };
  const run = (self, args) => Reflect.apply(get(), self, args);
  const swapped = new Proxy(original, { apply: (_, self, args) => (core.inTrace() ? run(self, args) : core.withTrace(() => run(self, args))) });
  if (module) {
    if (module.exports === original) module.exports = swapped;
    else if (module.exports) for (const k of Object.keys(module.exports)) if (module.exports[k] === original) module.exports[k] = swapped;
  }
  return swapped;
}

// ---------------------------------------------------------------- patterns

const patterns = {}; // the helpers pattern modules offer; a missing module takes only its own names away
for (const name of registry.NAMES) {
  try {
    const { PATTERN, ...names } = require(`./patterns/${name}.cjs`);
    Object.assign(patterns, names);
  } catch (e) {
    if (e.code !== "MODULE_NOT_FOUND") throw e;
  }
}

const { RejectedError, DeadlineExceeded, dependencyFailed, withTimeout, withDeadline, remainingMs, traceId, newTraceId, withTrace, sleep, sleepSync,
  seed, resetState, TRANSIENT_HTTP } = core;
const { retry, retrySync, RetryBudget, retryAfterS, attemptTimeout, CircuitBreaker, CircuitOpenError, Bulkhead, BulkheadFullError, AdaptiveThrottle,
  ThrottledError, AdaptiveConcurrency, ConcurrencyLimitError, hedge, StaleCache, idempotencyKey, isIdempotencyConflict,
  isIdempotencyMismatch } = patterns;

module.exports = { RasError, RejectedError, DeadlineExceeded, HttpError, Policy, retry, retrySync, RetryBudget, withTimeout, withDeadline, remainingMs,
  attemptTimeout, withFallback, CircuitBreaker, CircuitOpenError, Bulkhead, BulkheadFullError, AdaptiveThrottle, ThrottledError,
  AdaptiveConcurrency, ConcurrencyLimitError, hedge, StaleCache, idempotencyKey, isIdempotencyConflict, isIdempotencyMismatch,
  isTransient, dependencyFailed, raiseForStatus, retryAfterS, logEvent, traceId, newTraceId, withTrace, sleep, sleepSync, seed, resetState,
  scope, enable, TRANSIENT_HTTP };
