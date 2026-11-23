// Reliability helpers for hardened code. Node's built-ins only.
//
// Hardened files import these from "./_runtime.cjs" (ras-anything enable puts this file next to them):
//
//   import { retry, withTimeout, raiseForStatus, RasError, logEvent } from "./_runtime.cjs";
//
// Timers, Date and AbortSignal.timeout are looked up at call time, so `ras-anything verify` can run your code
// on a virtual clock. Randomness (jitter, trace ids) comes from one seedable generator: seed(n).
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

/** Thrown without calling the dependency while a breaker is open. */
class CircuitOpenError extends RasError {
  constructor(message, options) {
    super(message, options);
    this.name = "CircuitOpenError";
  }
}

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

// ---------------------------------------------------------------- transient errors

/** Timeouts, connection errors, EAGAIN, a child killed by its timeout or exiting 75, 408/425/429/500/502/503/504.
 *  Never other 4xx, never programming errors. */
function isTransient(err) {
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

/** The Retry-After delay (seconds) an HTTP error or Response asked for, if any. */
function retryAfterS(err) {
  const h = err?.headers;
  const value = typeof h?.get === "function" ? h.get("retry-after") : h?.["retry-after"];
  if (value === undefined || value === null || value === "") return null;
  const n = Number(value);
  if (Number.isFinite(n)) return Math.max(0, n);
  const at = Date.parse(value);
  return Number.isNaN(at) ? null : Math.max(0, (at - Date.now()) / 1000);
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

/** fn(signal), bounded to ms: pass the signal on (fetch, http.request, execFile take it). The promise rejects
 *  with a TimeoutError in any case, also when fn ignores the signal. */
async function withTimeout(fn, ms, { signal: outer } = {}) {
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

// ---------------------------------------------------------------- retry

function stepper({ attempts = 3, baseMs = 100, capMs = 2000, budgetMs, retryOn = isTransient, onRetry }) {
  if (!(attempts >= 1)) throw new RangeError("attempts must be >= 1");
  const start = nowMs();
  let n = 0;
  return {
    left: () => (budgetMs === undefined ? Infinity : budgetMs - (nowMs() - start)),
    /** ms to wait before the next attempt, or null to give up. */
    next(err) {
      n++;
      if (n >= attempts || !retryOn(err)) return null;
      let delay = random() * Math.min(capMs, baseMs * 2 ** (n - 1)); // "full jitter"
      const ra = retryAfterS(err);
      if (ra !== null) delay = Math.max(delay, ra * 1000);
      if (budgetMs !== undefined && nowMs() - start + delay >= budgetMs) return null;
      onRetry?.(n, err, delay);
      return delay;
    },
  };
}

/** Calls fn({attempt, signal}) up to `attempts` times, retrying only errors `retryOn` accepts.
 *
 *  Backoff is full jitter, at least the server's Retry-After. timeoutMs bounds each attempt (pass `signal` on),
 *  budgetMs the whole: attempts are cut to what is left and no wait crosses it. The last error is rethrown as is. */
async function retry(fn, options = {}) {
  const { timeoutMs } = options;
  const step = stepper(options);
  for (let attempt = 1; ; attempt++) {
    try {
      const limit = Math.min(timeoutMs ?? Infinity, Math.max(1, step.left()));
      return await (Number.isFinite(limit) ? withTimeout((signal) => fn({ attempt, signal }), limit) : fn({ attempt, signal: undefined }));
    } catch (e) {
      const delay = step.next(e);
      if (delay === null) throw e;
      await sleep(delay);
    }
  }
}

/** retry() for synchronous code (execFileSync, ...): fn({attempt, timeoutMs}); give timeoutMs to the call. */
function retrySync(fn, options = {}) {
  const step = stepper(options);
  for (let attempt = 1; ; attempt++) {
    try {
      const limit = Math.min(options.timeoutMs ?? Infinity, Math.max(1, step.left()));
      return fn({ attempt, timeoutMs: Number.isFinite(limit) ? Math.ceil(limit) : undefined });
    } catch (e) {
      const delay = step.next(e);
      if (delay === null) throw e;
      sleepSync(delay);
    }
  }
}

// ---------------------------------------------------------------- circuit breaker, fallback

/** Opens after failThreshold consecutive failures; after cooldownMs lets one trial call through (half-open). */
class CircuitBreaker {
  constructor({ failThreshold = 5, cooldownMs = 30_000, name = "dependency", count = isTransient } = {}) {
    Object.assign(this, { failThreshold, cooldownMs, name, count, failures: 0, openedAt: null, probing: false });
  }

  get state() {
    if (this.openedAt === null) return "closed";
    return nowMs() - this.openedAt >= this.cooldownMs ? "half_open" : "open";
  }

  reset() {
    Object.assign(this, { failures: 0, openedAt: null, probing: false });
  }

  async call(fn, ...args) {
    const st = this.state;
    if (st === "open" || (st === "half_open" && this.probing)) {
      throw new CircuitOpenError(`circuit open for ${this.name}`, { context: { failures: this.failures, cooldown_ms: this.cooldownMs } });
    }
    if (st === "half_open") this.probing = true;
    try {
      const v = await fn(...args);
      this.reset();
      return v;
    } catch (e) {
      if (st === "half_open") this.probing = false;
      if (this.count(e)) {
        this.failures++;
        if (st === "half_open" || this.failures >= this.failThreshold) this.openedAt = nowMs();
      }
      throw e;
    }
  }
}

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
  const swapped = new Proxy(original, { apply: (_, self, args) => (traces.getStore() ? run(self, args) : withTrace(() => run(self, args))) });
  if (module) {
    if (module.exports === original) module.exports = swapped;
    else if (module.exports) for (const k of Object.keys(module.exports)) if (module.exports[k] === original) module.exports[k] = swapped;
  }
  return swapped;
}

module.exports = { RasError, CircuitOpenError, HttpError, CircuitBreaker, retry, retrySync, withTimeout, withFallback, isTransient, raiseForStatus,
  retryAfterS, logEvent, traceId, newTraceId, withTrace, sleep, sleepSync, seed, scope, enable, TRANSIENT_HTTP };
