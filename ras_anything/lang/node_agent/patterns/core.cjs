// The core of the Node runtime: clock, randomness, trace ids, errors, logging, deadlines, what counts as
// transient. Pattern modules import this and nothing else (docs/PATTERNS.md). Node's built-ins only.
"use strict";
const { AsyncLocalStorage } = require("node:async_hooks");

const TRANSIENT_HTTP = new Set([408, 425, 429, 500, 502, 503, 504]);
const TRANSIENT_CODES = new Set(["ECONNREFUSED", "ECONNRESET", "ECONNABORTED", "EPIPE", "ETIMEDOUT", "ESOCKETTIMEDOUT", "EHOSTUNREACH",
  "ENETUNREACH", "ENETDOWN", "EAI_AGAIN", "EAGAIN", "UND_ERR_CONNECT_TIMEOUT", "UND_ERR_HEADERS_TIMEOUT", "UND_ERR_BODY_TIMEOUT",
  "UND_ERR_SOCKET"]);

// ---------------------------------------------------------------- randomness, trace ids

let state = (Math.random() * 2 ** 32) >>> 0;

/** Make jitter and trace ids reproducible. */
function seed(n) {
  state = Number(BigInt.asUintN(32, BigInt(Math.trunc(n))));
}
const SEED = globalThis[Symbol.for("ras-anything.seed")];
if (SEED !== undefined) seed(SEED);

function random() { // mulberry32
  state = (state + 0x6d2b79f5) >>> 0;
  let t = state;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 2 ** 32;
}

const traces = new AsyncLocalStorage();

function newTraceId() {
  const part = () => Math.floor(random() * 2 ** 32).toString(16).padStart(8, "0");
  return part() + part();
}

/** The current trace id (every call of an enabled function has its own), else a new one. */
function traceId() {
  return traces.getStore() ?? newTraceId();
}

/** Run fn with one trace id for everything logged and thrown inside (async included). */
function withTrace(fn, id = newTraceId()) {
  return traces.run(id, fn);
}

const inTrace = () => traces.getStore() !== undefined;

// ---------------------------------------------------------------- state

const stateful = new Set(); // WeakRefs to objects with reset()
const gone = new FinalizationRegistry((ref) => stateful.delete(ref));

/** Register an object whose state (tokens, open circuits, caches) resetState() clears. */
function track(obj) {
  const ref = new WeakRef(obj);
  stateful.add(ref);
  gone.register(obj, ref);
  return obj;
}

/** Forget what every breaker, budget, throttle, bulkhead and cache has seen so far. (verify does this before
 *  each independent trial; tests may want it too.) */
function resetState() {
  for (const ref of stateful) ref.deref()?.reset();
}

// ---------------------------------------------------------------- errors

/** A structured error: what failed, why (cause), where (context) and a trace id to grep for. */
class RasError extends Error {
  constructor(message, { cause, context = {}, traceId: id } = {}) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "RasError";
    this.context = { ...context };
    this.traceId = id ?? traceId();
  }

  toString() {
    const ctx = Object.entries(this.context).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ");
    const cause = this.cause ? `cause=${this.cause.name}: ${this.cause.message}` : "";
    return [`${this.name}: ${this.message}`, ctx, cause, `trace_id=${this.traceId}`].filter(Boolean).join(" | ");
  }

  toJSON() {
    return { error: this.name, message: this.message, cause: this.cause ? `${this.cause.name}: ${this.cause.message}` : null,
      context: this.context, trace_id: this.traceId };
  }
}

/** A pattern of this runtime refused to call the dependency (open circuit, full bulkhead, load shed, ...). */
class RejectedError extends RasError {
  constructor(message, options) {
    super(message, options);
    this.name = "RejectedError";
  }
}

/** The caller's deadline has passed: there is no time left for (another) attempt. */
class DeadlineExceeded extends Error {
  constructor(message = "the deadline has passed") {
    super(message);
    this.name = "DeadlineExceeded";
    this.code = "ETIMEDOUT";
  }
}

/** One JSON line on stderr: {"ts", "event", "trace_id", ...fields}. */
function logEvent(event, fields = {}) {
  const { trace_id: id, ...rest } = fields;
  const rec = { ts: Math.round(Date.now()) / 1000, event, trace_id: id ?? traceId() };
  for (const [k, v] of Object.entries(rest)) {
    rec[k] = v === null || ["string", "number", "boolean"].includes(typeof v) ? v : v instanceof Error ? `${v.name}: ${v.message}` : safeJson(v);
  }
  try {
    process.stderr.write(JSON.stringify(rec) + "\n");
  } catch {} // logging must never break the caller
}

function safeJson(v) {
  try {
    return JSON.stringify(v) ?? String(v);
  } catch {
    return String(v);
  }
}

// ---------------------------------------------------------------- what a failure means

/** Timeouts, connection errors, EAGAIN, a child killed by its timeout or exiting 75, 408/425/429/500/502/503/504.
 *  Never other 4xx, never programming errors. */
function isTransient(err) {
  if (err instanceof RejectedError) return false;
  for (let e = err, depth = 0; e && depth < 5; e = e.cause, depth++) {
    const status = typeof e.status === "number" ? e.status : typeof e.statusCode === "number" ? e.statusCode : null;
    if (e.name === "TimeoutError" || e.code === "ETIMEDOUT") return true;
    if (status !== null && status >= 100) return TRANSIENT_HTTP.has(status);
    if (TRANSIENT_CODES.has(e.code)) return true;
    if (e.code === 75 || e.status === 75 || (e.killed === true && e.signal)) return true; // child_process
    if (e.message === "terminated" && e.name === "TypeError") return true; // fetch: the body broke off
  }
  return false;
}

/** Did the dependency not take this request? (what throttles and concurrency limits count: transient errors, but not
 *  our own deadline running out before the dependency was asked) */
function notAccepted(err) {
  return isTransient(err) && !(err instanceof DeadlineExceeded);
}

/** Is the dependency in trouble? (what circuit breakers count: notAccepted, except a 429: it is up, and asks us to
 *  come back later) */
function dependencyFailed(err) {
  return notAccepted(err) && httpStatus(err) !== 429;
}

/** The HTTP status an error carries, or null. */
function httpStatus(err) {
  const s = typeof err?.status === "number" ? err.status : err?.statusCode;
  return typeof s === "number" && s >= 100 ? s : null;
}

// ---------------------------------------------------------------- time

const nowMs = () => performance.now();

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/** Blocks the process: only for synchronous functions. */
function sleepSync(ms) {
  if (ms > 0) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

const deadlines = new AsyncLocalStorage(); // absolute, on the nowMs() clock

/** Milliseconds left before the innermost deadline around this code, or null. */
function remainingMs() {
  const d = deadlines.getStore();
  return d === undefined || d === null ? null : Math.max(0, d - nowMs());
}

/** fn() with a deadline at `at` (absolute nowMs), or the one already around it if that is earlier. */
function runUntil(at, fn) {
  if (at === null || at === undefined) return fn();
  const cur = deadlines.getStore() ?? Infinity;
  return deadlines.run(Math.min(cur, at), fn);
}

/** fn() with everything inside sharing one deadline, ms from now; a nested deadline can only be earlier. It
 *  travels with the async context. withTimeout(), retry() and Policy honour it. */
function withDeadline(fn, ms) {
  return runUntil(nowMs() + ms, fn);
}

/** fn(signal), bounded to ms and to the deadline around it: pass the signal on (fetch, http.request, execFile
 *  take it). The promise rejects with a TimeoutError in any case, also when fn ignores the signal. */
async function withTimeout(fn, ms, { signal: outer } = {}) {
  const left = remainingMs();
  if (left !== null && left <= 0) throw new DeadlineExceeded();
  if (left !== null) ms = Math.min(ms, left);
  const limit = AbortSignal.timeout(Math.max(1, Math.ceil(ms)));
  const signal = outer ? AbortSignal.any([limit, outer]) : limit;
  signal.throwIfAborted();
  const keepAlive = setTimeout(() => {}, ms + 50); // AbortSignal.timeout doesn't keep the process running
  let stop;
  const aborted = new Promise((_, reject) => {
    stop = () => reject(signal.reason);
    signal.addEventListener("abort", stop, { once: true });
  });
  try {
    const work = Promise.resolve().then(() => fn(signal));
    work.catch(() => {}); // when the limit wins, its late failure isn't news
    return await Promise.race([work, aborted]);
  } finally {
    clearTimeout(keepAlive);
    signal.removeEventListener("abort", stop);
  }
}

// ---------------------------------------------------------------- the contract of a pattern module

const ADOPTED = "adopted";
const OPT_IN = "opt-in";
const REJECTED = "rejected";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS", "TRACE", "RUN"]); // RUN: a subprocess, like the recordings call it

/** One pattern inside one Policy (one dependency). State lives here; every call passes through.
 *
 *  run(call, inner) is a generator. `yield* inner(call)` is the rest of the policy: its value is the result, its
 *  exceptions are the failures. Code never sleeps or calls the dependency itself: it yields an operation and the
 *  driver (async or sync) carries it out: ["call", attempt, untilMs], ["sleep", ms], ["race", make, delayMs, gate]. */
class Layer {
  constructor(dependency, params) {
    this.dependency = dependency;
    this.params = params;
  }

  * run(call, inner) {
    return yield* inner(call);
  }
}

/** One logical call on its way through a policy: what the layers tell each other. */
class Call {
  constructor(dependency, { method = "GET", idempotencyKey = null, cacheKey = null } = {}) {
    Object.assign(this, { dependency, method, idempotencyKey, cacheKey, deadline: null, timeoutMs: null, attempts: 0, headers: {}, carryKey: null,
      retryGates: [], invited: false, delayRules: [], retryRules: [] });
  }

  get safe() {
    return SAFE_METHODS.has(this.method.toUpperCase());
  }

  /** May it be sent more than once? (a read, or every attempt carries one Idempotency-Key) */
  get repeatable() {
    return this.safe || this.carryKey !== null;
  }
}

/** call()/callSync() around acquire()/release(): what breakers, bulkheads and limiters share. */
class Guard {
  async call(fn, ...args) {
    const ticket = this.acquire();
    let error;
    try {
      return await fn(...args);
    } catch (e) {
      error = { e };
      throw e;
    } finally {
      this.release(ticket, error?.e);
    }
  }

  callSync(fn, ...args) {
    const ticket = this.acquire();
    let error;
    try {
      return fn(...args);
    } catch (e) {
      error = { e };
      throw e;
    } finally {
      this.release(ticket, error?.e);
    }
  }

  /** The same for a layer: the rest of the policy as a generator. */
  * around(call, inner) {
    const ticket = this.acquire();
    let error;
    try {
      return yield* inner(call);
    } catch (e) {
      error = { e };
      throw e;
    } finally {
      this.release(ticket, error?.e);
    }
  }
}

module.exports = { TRANSIENT_HTTP, seed, random, newTraceId, traceId, withTrace, inTrace, track, resetState, RasError, RejectedError, DeadlineExceeded,
  logEvent, isTransient, dependencyFailed, notAccepted, httpStatus, nowMs, sleep, sleepSync, remainingMs, runUntil, withDeadline, withTimeout,
  ADOPTED, OPT_IN, REJECTED, SAFE_METHODS, Layer, Call, Guard };
