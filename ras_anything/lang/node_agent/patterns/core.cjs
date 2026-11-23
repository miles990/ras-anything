// The core of the Node runtime: clock, randomness, trace context, errors, diagnostics, health, deadlines, what counts
// as transient. Pattern modules import this and nothing else (docs/PATTERNS.md). Node's built-ins only.
"use strict";
const { AsyncLocalStorage } = require("node:async_hooks");
const path = require("node:path");

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

function mulberry32(s) {
  let t = s;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 2 ** 32;
}

function randomFromProcess() {
  state = (state + 0x6d2b79f5) >>> 0;
  return mulberry32(state);
}

/** Randomness for jitter: the call's own generator (so an incident can be replayed), else the process's. */
function random() {
  const ctx = context();
  if (!ctx) return randomFromProcess();
  ctx.rng = (ctx.rng + 0x6d2b79f5) >>> 0;
  return mulberry32(ctx.rng);
}

// ---------------------------------------------------------------- trace context (W3C)

const traces = new AsyncLocalStorage(); // {traceId, spanId, ctx}
const context = () => traces.getStore()?.ctx ?? null; // the call of a hardened function this runs in

const hex = (words) => Array.from({ length: words }, () => Math.floor(randomFromProcess() * 2 ** 32).toString(16).padStart(8, "0")).join("");
const newTraceId = () => hex(4);
const newSpanId = () => hex(2);

/** [trace id, span id] of a W3C `traceparent`, or null if it isn't a valid one. */
function parseTraceparent(value) {
  const m = typeof value === "string" ? /^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}(-.*)?$/.exec(value.trim().toLowerCase()) : null;
  return !m || m[1] === "ff" || /^0+$/.test(m[2]) || /^0+$/.test(m[3]) ? null : [m[2], m[3]];
}

/** The current trace id (every call of an enabled function has its own), else a new one. */
function traceId() {
  return traces.getStore()?.traceId ?? parseTraceparent(process.env.TRACEPARENT)?.[0] ?? newTraceId();
}

const spanId = () => traces.getStore()?.spanId ?? null;

/** Run fn with one trace id for everything logged and thrown inside (async included). `id`: a trace id, or an
 *  incoming `traceparent` header. */
function withTrace(fn, id = null) {
  const outer = parseTraceparent(id) ?? (id === null ? parseTraceparent(process.env.TRACEPARENT) : null);
  return traces.run({ traceId: outer?.[0] ?? id ?? newTraceId(), spanId: outer?.[1] ?? newSpanId(), ctx: null }, fn);
}

const inTrace = () => traces.getStore() !== undefined;

/** The W3C `traceparent` to send along with a dependency call, so its logs and ours share a trace. */
const traceparent = () => `00-${traceId()}-${spanId() ?? newSpanId()}-01`;
const traceHeaders = () => ({ traceparent: traceparent() });

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
  functions.clear();
  tripped.clear();
}

// ---------------------------------------------------------------- masking

const MASKS = [
  [/(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}/gi, "[token]"],
  [/\beyJ[\w-]{8,}\.[\w-]+\.[\w-]*/g, "[token]"],
  [/\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|xox[abprs]|AKIA|ASIA|AIza)[-_A-Za-z0-9]{12,}/g, "[token]"],
  [/\b((?:api[_-]?key|access[_-]?key|token|secret|passw(?:or)?d|pwd|authorization|session|signature|sig)["']?\s*[=:]\s*["']?)[^\s"'&,;}]+/gi, "$1[secret]"],
  [/(?<=:\/\/)[^/\s:@]+:[^/\s@]+@/g, "[secret]@"],
  [/[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g, "[email]"],
];
const CARD = /(?<![\w.-])\d(?:[ -]?\d){12,18}(?![\w-]|\.\d)/g;
const PHONE = /(?<![\w.:/-])(?<!\d[ .-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?|\d{2,4}[ .-])\d{3,4}[ .-]\d{3,4}(?![\w-]|\.\d|[ .-]\d)|(?<![\w.:/-])\+\d{8,14}(?!\w)/g;

function luhn(text) {
  let sum = 0;
  [...text.replace(/\D/g, "")].reverse().forEach((c, i) => {
    const x = Number(c);
    sum += i % 2 === 0 ? x : x > 4 ? 2 * x - 9 : 2 * x;
  });
  return sum % 10 === 0;
}

/** Text without e-mail addresses, phone and card numbers, tokens and secrets. */
function mask(text) {
  for (const [rx, repl] of MASKS) text = text.replace(rx, repl);
  return text.replace(CARD, (m) => (luhn(m) ? "[card]" : m)).replace(PHONE, "[phone]");
}

const INTERNALS = [
  [/Traceback \(most recent call last\):[\s\S]*/g, "[trace]"],
  [/\bFile "[^"]+", line \d+(?:, in \S+)?/g, "[path]"],
  [/\n\s+at [\s\S]*/g, ""],
  [/\b[a-z][a-z0-9+.-]*:\/\/[^\s"'<>)\]]+/gi, "[url]"],
  [/(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?![\w.])/g, "[host]"],
  [/\[?(?<![\w:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{1,4}\]?(?::\d+)?(?![\w:])/gi, "[host]"],
  [/\blocalhost(?::\d+)?\b/g, "[host]"],
  [/\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:com|net|org|io|dev|app|local|internal|lan|corp|example|test|cloud|ai|co|svc|cluster)\b(?::\d+)?/gi, "[host]"],
  [/(?<![\w.])(?:~|\.{1,2})?\/(?:[\w.@+-]+\/)+[\w.@+-]*/g, "[path]"],
  [/\b[A-Za-z]:\\\\?[^\s"']+/g, "[path]"],
  [/(?<![\w./])[\w-]+\.(?:py|mjs|cjs|js|ts|go|rb|java|php|cs)(?::\d+)?\b/g, "[path]"],
];

/** mask(), and without the inside of this system either: hosts, URLs, paths, stack frames. For people and agents
 *  outside; logs keep them. */
function plain(text, limit = 200) {
  text = mask(String(text));
  for (const [rx, repl] of INTERNALS) text = text.replace(rx, repl);
  text = text.split(/\s+/).filter(Boolean).join(" ");
  return text.length <= limit ? text : text.slice(0, limit - 1) + "…";
}

// ---------------------------------------------------------------- errors

const lazy = (name) => {
  try {
    return require(`./${name}.cjs`);
  } catch {
    return {};
  }
};

/** A structured error. String(e) is one short sentence anyone may see; e.problem (and JSON.stringify(e)) is the same for
 *  an agent (RFC 9457 problem+json: what failed, is a retry worth it, what to do next); `context` and the cause are for our
 *  own logs (toDict()).
 *
 *  Cause, dependency and advice are worked out from `cause` and the call in progress; options override them: dependency,
 *  retryable, retryAfterS, nextAction, needsHumanAuthorization, category, httpStatus. */
class RasError extends Error {
  constructor(message, { cause, context: facts = {}, traceId: id, ...advice } = {}) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "RasError";
    const ctx = context();
    const hit = ctx ? ctx.lastDependency ?? (ctx.deps.length === 1 ? ctx.deps[0].name : null) : null;
    if (hit && advice.dependency === undefined) advice.dependency = hit;
    Object.defineProperty(this, "context", { value: { ...facts }, writable: true, configurable: true }); // (not for console.log(e))
    Object.defineProperty(this, "advice", { value: advice, writable: true, configurable: true });
    this.traceId = id ?? traceId();
  }

  get problem() {
    const { problem } = lazy("problem");
    if (problem) return problem(this);
    return { type: "about:blank", title: "Error", status: 500, detail: plain(this.message), instance: this.traceId, correlation_id: this.traceId };
  }

  toString() {
    const p = this.problem;
    const why = [p.cause && p.cause !== "unknown" ? `cause: ${p.cause}` : "", p.next_action ? `next: ${p.next_action}` : ""].filter(Boolean).join("; ");
    return `${this.name}: ${plain(this.message)} (${why ? `${why}; ` : ""}trace_id=${this.traceId})`;
  }

  toJSON() {
    return this.problem;
  }

  toDict() {
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

// ---------------------------------------------------------------- calls in progress

/** One call of a hardened function: its span, what it may call, and what happened in it (for the flight recorder). */
class CallContext {
  constructor(fid, deps, trace, span, parent, args) {
    Object.assign(this, { fid, deps, traceId: trace, spanId: span, parent, args, events: [], dropped: 0, t0: nowMs(), ts: Date.now(), gaveUp: null,
      lastDependency: null, lastTarget: null, health: tripped.size ? [...tripped.values()] : [] });
    this.seed = Math.floor(randomFromProcess() * 2 ** 32);
    this.rng = this.seed;
  }

  note(event, facts = {}) {
    if (this.events.length >= 200) {
      this.dropped++;
      return;
    }
    const e = { t: round3((nowMs() - this.t0) / 1000), event };
    for (const [k, v] of Object.entries(facts)) if (v !== null && v !== undefined) e[k] = v;
    this.events.push(e);
  }
}

const round3 = (x) => Math.round(x * 1000) / 1000;
const functions = new Map(); // id -> counters, for health()
const tripped = new Map(); // what isn't at rest (open breakers)

/** Calls fn as the hardened function `fid`, with a span of its own; `deps` (what recording saw it call) lets diagnostics
 *  name the dependency that failed. */
function observe(fid, deps, fn, self, args) {
  let ctx;
  try {
    const store = traces.getStore();
    ctx = new CallContext(fid, deps ?? [], traceId(), newSpanId(), store?.spanId ?? parseTraceparent(process.env.TRACEPARENT)?.[1] ?? null, args);
  } catch {
    return Reflect.apply(fn, self, args);
  }
  return traces.run({ traceId: ctx.traceId, spanId: ctx.spanId, ctx }, () => {
    let r;
    try {
      r = Reflect.apply(fn, self, args);
    } catch (e) {
      ended(ctx, "raise", e);
      throw e;
    }
    if (r === null || typeof r !== "object" || typeof r.then !== "function") {
      ended(ctx, "return", null);
      return r;
    }
    return r.then((v) => {
      ended(ctx, "return", null);
      return v;
    }, (e) => {
      ended(ctx, "raise", e);
      throw e;
    });
  });
}

function ended(ctx, outcome, error) {
  try {
    const st = functions.get(ctx.fid) ?? { calls: 0, raised: 0, gave_up: 0, last_fault: null, last_incident: null };
    functions.set(ctx.fid, st);
    st.calls++;
    if (outcome === "raise") st.raised++;
    if (ctx.gaveUp !== null) st.gave_up++;
    if (ctx.gaveUp !== null || outcome === "raise") st.last_fault = ctx.gaveUp ?? faultClass(error);
    st.last_incident = lazy("recorder").finish?.(ctx, outcome, error) ?? st.last_incident;
  } catch {} // recording must never break the caller
  writeHealth();
}

// ---------------------------------------------------------------- diagnostics (OpenTelemetry log data model)

const SEVERITY = { DEBUG: 5, INFO: 9, WARN: 13, ERROR: 17 };
const LEVEL = { off: 99, error: 17, warn: 13, info: 9, debug: 5 };
const SLOW_S = 1; // an answered attempt that took longer than this is reported ($RAS_ANYTHING_SLOW_S)
const SERVICE = process.env.OTEL_SERVICE_NAME || path.basename(process.argv[1] ?? "") || "node";

function logValue(v) {
  if (v === null || ["number", "boolean"].includes(typeof v)) return v;
  return mask(typeof v === "string" ? v : v instanceof Error ? `${v.name}: ${v.message}` : safeJson(v));
}

function noteTarget(target) {
  const ctx = context();
  if (!ctx || !target) return;
  ctx.lastTarget = target;
  where(null);
}
globalThis[Symbol.for("ras-anything.noteTarget")] = noteTarget;

/** One log record in the OpenTelemetry log data model, as a JSON line on stderr. */
function emit(event, severity = "INFO", body = null, attributes = {}, { legacy = false, trace = null } = {}) {
  try {
    if (SEVERITY[severity] < (LEVEL[(process.env.RAS_ANYTHING_LOG || "info").toLowerCase()] ?? 9) && !legacy) return;
    const now = Date.now();
    const ctx = context();
    const tid = trace ?? traceId();
    const rec = legacy ? { ts: Math.round(now) / 1000, event, trace_id: tid } : {}; // (the keys logEvent has always had)
    Object.assign(rec, { Timestamp: `${Math.round(now)}000000`, SeverityText: severity, SeverityNumber: SEVERITY[severity], EventName: event, TraceId: tid });
    if (spanId()) rec.SpanId = spanId();
    Object.assign(rec, { Body: mask(body ?? event), Resource: { "service.name": SERVICE } });
    const attrs = {};
    for (const [k, v] of Object.entries(attributes)) if (v !== undefined && (v !== null || legacy)) attrs[k] = logValue(v);
    if (ctx) attrs["code.function"] ??= ctx.fid;
    rec.Attributes = attrs;
    process.stderr.write(JSON.stringify(rec) + "\n");
  } catch {} // logging must never break the caller
}

/** One JSON line on stderr for something this code decided (a give-up, a fallback): `event`, `trace_id`, and the fields
 *  as attributes, plus the OpenTelemetry log record around them. */
function logEvent(event, fields = {}) {
  const { trace_id: id, ...rest } = fields;
  const ctx = context();
  try {
    if (rest.error instanceof Error) Object.assign(rest, errorAttributes(rest.error, rest.dependency));
    else if (ctx?.lastDependency) rest["ras.dependency"] ??= ctx.lastDependency;
    ctx?.note("log", { name: event });
  } catch {}
  const info = ["ras.approval.granted", "ras.shadow.skip"].some((x) => event.startsWith(x));
  emit(event, info ? "INFO" : "WARN", null, rest, { legacy: true, trace: id ?? null });
}

function safeJson(v) {
  try {
    return JSON.stringify(v) ?? String(v);
  } catch {
    return String(v);
  }
}

function statusClass(status) {
  if (status === 429) return "rate_limited";
  if ([401, 403, 407].includes(status)) return "auth";
  if (status === 402) return "quota";
  if (status === 408 || status === 504) return "timeout";
  if ([409, 412, 423].includes(status)) return "conflict";
  if (status === 501 || status === 505) return "unsupported";
  if (status >= 400 && status < 500) return status === 425 ? "unavailable" : "invalid_request";
  return status >= 500 ? "unavailable" : null;
}

const CODE_CLASS = { ETIMEDOUT: "timeout", ESOCKETTIMEDOUT: "timeout", UND_ERR_CONNECT_TIMEOUT: "timeout", UND_ERR_HEADERS_TIMEOUT: "timeout",
  UND_ERR_BODY_TIMEOUT: "timeout", ECONNREFUSED: "refused", EAGAIN: "refused", ENOTFOUND: "refused", EAI_AGAIN: "refused", ECONNRESET: "unavailable",
  ECONNABORTED: "unavailable", EPIPE: "unavailable", EHOSTUNREACH: "unavailable", ENETUNREACH: "unavailable", ENETDOWN: "unavailable",
  UND_ERR_SOCKET: "unavailable" };

/** What a failure was, in the words of the fault model: timeout, refused, unavailable, rate_limited, truncated,
 *  invalid_request, auth, conflict, quota, circuit_open, rejected, approval, unsupported, bug or unknown. */
function faultClass(err) {
  for (let e = err, depth = 0; e !== null && typeof e === "object" && depth < 6; e = e.cause, depth++) {
    const c = classOf(e);
    if (c) return c;
  }
  return err instanceof TypeError || err instanceof ReferenceError || err instanceof RangeError ? "bug" : "unknown";
}

function classOf(e) {
  if (e instanceof RejectedError) return e.name === "CircuitOpenError" ? "circuit_open" : "rejected";
  if (e.needsHumanAuthorization) return "approval";
  const status = httpStatus(e);
  if (status !== null) {
    const c = statusClass(status);
    return c === "rate_limited" && /quota/i.test(`${e.body ?? ""} ${e.message ?? ""}`) ? "quota" : c;
  }
  if (e.name === "TimeoutError") return "timeout";
  if (CODE_CLASS[e.code]) return CODE_CLASS[e.code];
  if (Array.isArray(e.errors) && e.errors[0]) return classOf(e.errors[0]);
  if (e.code === 75 || e.status === 75 || (e.killed === true && e.signal && e.name !== "AbortError")) return e.killed === true && e.signal ? "timeout" : "unavailable";
  if (e.message === "terminated" && e.name === "TypeError") return "truncated";
  if (e instanceof SyntaxError || e.name === "SyntaxError") return "truncated";
  return null;
}

function targetOf(err) {
  for (let e = err, depth = 0; e !== null && typeof e === "object" && depth < 6; e = e.cause, depth++) {
    if (typeof e.url === "string" && e.url.includes("://")) return { url: e.url };
    if (typeof e.cmd === "string" && e.cmd) return { argv: e.cmd.split(" ") };
    if (Array.isArray(e.spawnargs)) return { argv: [e.path ?? "", ...e.spawnargs] };
    const inner = Array.isArray(e.errors) ? e.errors[0] : e;
    if (inner?.address ?? inner?.hostname) return { address: inner.address ?? inner.hostname, port: inner.port };
  }
  return {};
}

/** The dependency a failure (or a slow answer) belongs to, as attributes. */
function where(err, dependency) {
  const ctx = context();
  const deps = ctx?.deps ?? [];
  let target = err ? targetOf(err) : {};
  if (!target.url && !target.argv && !target.address && ctx?.lastTarget) target = ctx.lastTarget;
  const out = {};
  let hit = deps.length === 1 ? deps[0] : null;
  if (target.url) {
    const u = new URL(target.url);
    Object.assign(out, { "server.address": u.hostname, "server.port": u.port ? Number(u.port) : undefined, "url.path": u.pathname });
    hit = deps.find((d) => d.route && u.pathname.startsWith(d.route)) ?? hit;
  } else if (target.argv) {
    const words = target.argv.slice(0, 4).map((w) => path.basename(String(w)));
    out["process.command"] = words.join(" ");
    hit = deps.find((d) => d.program && words.includes(d.program)) ?? hit;
  } else if (target.address) Object.assign(out, { "server.address": target.address, "server.port": target.port });
  if (hit) {
    out["url.template"] ??= hit.route ?? undefined;
    out["process.command"] ??= hit.program ?? undefined;
    out["http.request.method"] = hit.method ?? undefined;
  }
  const name = dependency ?? hit?.name;
  if (name) {
    out["ras.dependency"] = name;
    if (ctx) ctx.lastDependency = hit?.name ?? name;
  }
  return out;
}

function errorAttributes(err, dependency) {
  const root = err instanceof RasError && err.cause ? err.cause : err;
  const status = httpStatus(root);
  return { "error.type": status !== null ? String(status) : root?.cause?.code ?? root?.code ?? root?.name ?? typeof root,
    "http.response.status_code": status ?? undefined, "ras.fault": faultClass(err), "exception.message": plain(String(root?.message ?? root), 200),
    ...where(err, dependency) };
}

function advice(err) {
  try {
    const p = lazy("problem").problem(err);
    return { "ras.cause": p.cause, "ras.retryable": p.retryable, "ras.next_action": p.next_action, "ras.retry_after_s": p.retry_after_s };
  } catch {
    return {};
  }
}

const reported = new WeakMap(); // error -> attempt it was reported for
const final = new WeakSet();

/** One attempt of a dependency call failed (called by retry and Policy). */
function attemptFailed(err, n, tookMs, { dependency = null, key = null } = {}) {
  try {
    if (err instanceof RejectedError || (typeof err === "object" && err !== null && reported.get(err) === n)) return;
    const attrs = errorAttributes(err, dependency ?? undefined);
    context()?.note("failure", { n, dependency: attrs["ras.dependency"], fault: attrs["ras.fault"], took_s: round3(tookMs / 1000), error: attrs["error.type"],
      retry_after_s: lazy("retry_after").retryAfterS?.(err) });
    emit("ras.dependency.failure", "WARN", `${attrs["ras.dependency"] ?? "a dependency"} failed: ${attrs["ras.fault"]} (attempt ${n})`,
      { ...attrs, "http.request.resend_count": n - 1, "ras.duration_s": round3(tookMs / 1000), "ras.idempotency_key": key });
    if (typeof err === "object" && err !== null) reported.set(err, n);
  } catch {}
}

/** Attempt n failed and another follows after delayMs. */
function retrying(err, n, delayMs, { dependency = null } = {}) {
  try {
    const ctx = context();
    ctx?.note("retry", { n, delay_s: round3(delayMs / 1000) });
    emit("ras.retry", "INFO", `retry ${n} in ${round3(delayMs / 1000)}s`, { "http.request.resend_count": n, "ras.retry.delay_s": round3(delayMs / 1000),
      "ras.dependency": dependency ?? ctx?.lastDependency });
  } catch {}
}

/** No further attempt: the caller gets the error (or degrades). */
function gaveUp(err, n, why, { dependency = null, key = null } = {}) {
  try {
    if (typeof err === "object" && err !== null && final.has(err)) return;
    const attrs = errorAttributes(err, dependency ?? undefined);
    const ctx = context();
    if (ctx) {
      ctx.gaveUp = attrs["ras.fault"];
      ctx.note("gave_up", { n, why, fault: attrs["ras.fault"], dependency: attrs["ras.dependency"] });
    }
    emit("ras.dependency.gave_up", "ERROR", `${attrs["ras.dependency"] ?? "a dependency"}: gave up after ${n} attempt(s): ${attrs["ras.fault"]} (${why})`,
      { ...attrs, "http.request.resend_count": Math.max(0, n - 1), "ras.give_up": why, "ras.idempotency_key": key, ...advice(err) });
    if (typeof err === "object" && err !== null) final.add(err);
  } catch {}
}

/** An attempt was answered: worth a record if it was slow, or the one that worked after failures. */
function attemptOk(n, tookMs, { dependency = null } = {}) {
  try {
    const limit = 1000 * Number(process.env.RAS_ANYTHING_SLOW_S || SLOW_S);
    if (tookMs < limit && n <= 1) return;
    const ctx = context();
    const attrs = where(null, dependency ?? undefined);
    if (tookMs >= limit) {
      ctx?.note("slow", { n, took_s: round3(tookMs / 1000), dependency: attrs["ras.dependency"] });
      emit("ras.dependency.slow", "WARN", `${attrs["ras.dependency"] ?? "a dependency"} answered after ${round3(tookMs / 1000)}s`,
        { ...attrs, "ras.fault": "slow", "ras.duration_s": round3(tookMs / 1000), "http.request.resend_count": n - 1 });
    }
    if (n > 1) {
      ctx?.note("recovered", { n });
      emit("ras.dependency.recovered", "INFO", `answered on attempt ${n}`, { ...attrs, "http.request.resend_count": n - 1 });
    }
  } catch {}
}

/** A breaker (or another guard) changed state. */
function transition(kind, name, before, after, facts = {}) {
  try {
    if (after === "closed") tripped.delete(`${kind} ${name}`);
    else tripped.set(`${kind} ${name}`, { kind, name, state: after, since: round3(nowMs() / 1000) });
    context()?.note(kind, { name, before, after });
    emit(`ras.${kind}.transition`, after === "closed" ? "INFO" : "WARN", `${kind} ${name}: ${before} -> ${after}`,
      { "ras.dependency": name, [`ras.${kind}.state`]: after, [`ras.${kind}.previous_state`]: before, ...facts });
    writeHealth(true);
  } catch {}
}

// ---------------------------------------------------------------- health

/** For a health endpoint: {live, ready, status: "ok" | "degraded", breakers, functions}. Degraded (and not ready) while a
 *  breaker is open. `live` only says this code runs. */
function health() {
  const states = [];
  for (const ref of stateful) {
    try {
      const h = ref.deref()?.health?.();
      if (h) states.push(h);
    } catch {}
  }
  const breakers = states.filter((s) => s.kind === "breaker");
  const down = breakers.filter((b) => b.state === "open");
  return { schema: "ras-anything.health/1", time: Date.now() / 1000, service: SERVICE, pid: process.pid, live: true, ready: down.length === 0,
    status: down.length ? "degraded" : "ok", disabled: process.env.RAS_ANYTHING_DISABLE === "1", breakers,
    guards: states.filter((s) => s.kind !== "breaker"), functions: Object.fromEntries([...functions].map(([k, v]) => [k, { ...v }])) };
}

let healthWritten = -Infinity;

/** health() as a file, when $RAS_ANYTHING_HEALTH_FILE is set: on every transition, else once a second at most. */
function writeHealth(force = false) {
  const file = process.env.RAS_ANYTHING_HEALTH_FILE;
  if (!file || (!force && Math.abs(nowMs() - healthWritten) < 1000)) return;
  healthWritten = nowMs();
  try {
    const fs = require("node:fs");
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(health()));
    fs.renameSync(tmp, file);
  } catch {}
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

module.exports = { TRANSIENT_HTTP, seed, random, newTraceId, newSpanId, traceId, spanId, withTrace, inTrace, parseTraceparent, traceparent, traceHeaders,
  mask, plain, context, observe, emit, faultClass, attemptFailed, attemptOk, retrying, gaveUp, transition, health, SERVICE, track, resetState, RasError, RejectedError, DeadlineExceeded,
  logEvent, isTransient, dependencyFailed, notAccepted, httpStatus, nowMs, sleep, sleepSync, remainingMs, runUntil, withDeadline, withTimeout,
  ADOPTED, OPT_IN, REJECTED, SAFE_METHODS, Layer, Call, Guard };
