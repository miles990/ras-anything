// Flight recorder: what the last calls of hardened functions did, in memory; written out as an incident when one of them
// could not be answered (threw, or gave up on a dependency). The same limits, redaction and document as in Python
// (patterns/recorder.py). Nothing is written unless there is a .ras/ folder (or $RAS_ANYTHING_INCIDENTS names one);
// $RAS_ANYTHING_INCIDENTS=off switches it off.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");
const core = require("./core.cjs");
const SCHEMA_VERSION = require("./schema_version.cjs");

const RING = 64;
const BEFORE = 20;
const PER_KIND = 5;
const PER_PROCESS = 100;
const KEEP_FILES = 200;
const ring = [];
const written = new Map();

// ---------------------------------------------------------------- inputs

const SENSITIVE = new Set(`password passwd pwd secret token auth authorization credential credentials apikey key session cookie signature pin otp cvv
cvc card pan iban account ssn passport dob birthday birthdate email mail phone mobile tel fax address street zip postcode city name firstname
lastname fullname surname username user customer patient recipient ip`.split(/\s+/));
const PUBLIC = new Set(`idempotency_key key_id sort_key cache_key primary_key filename file_name hostname app_name service_name city_id user_id
customer_id account_id`.split(/\s+/));

const parts = (name) => String(name).replace(/(?<=[a-z0-9])(?=[A-Z])/g, "_").toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);

/** Is a value with this name one to keep out of the record? */
function sensitive(name) {
  const words = parts(name);
  const joined = words.join("_");
  const extra = (process.env.RAS_ANYTHING_REDACT ?? "").split(",").map((w) => w.trim().toLowerCase()).filter(Boolean);
  if (extra.includes(joined) || words.some((w) => extra.includes(w))) return true;
  return !PUBLIC.has(joined) && words.some((w) => SENSITIVE.has(w));
}

/** A copy of a value that may be written down: sensitive names are dropped, text is masked, size is bounded. */
function redact(value, name = null, depth = 0) {
  if (name !== null && value !== null && value !== undefined && sensitive(name)) return "[redacted]";
  if (value === null || value === undefined) return null;
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return core.mask(String(value)) === String(value) ? value : "[redacted]";
  if (typeof value === "string") {
    const text = core.mask(value);
    return text.length <= 200 ? text : `${text.slice(0, 200)}… (${text.length} characters)`;
  }
  if (ArrayBuffer.isView(value)) return `<${value.byteLength} bytes>`;
  if (typeof value !== "object" || depth >= 4) return `<${typeof value === "object" ? value.constructor?.name ?? "object" : typeof value}>`;
  if (Array.isArray(value) || value instanceof Set) {
    const items = [...value];
    return [...items.slice(0, 50).map((v) => redact(v, name, depth + 1)), ...(items.length > 50 ? [`… ${items.length - 50} more`] : [])];
  }
  const entries = value instanceof Map ? [...value] : Object.entries(value);
  const out = {};
  for (const [k, v] of entries.slice(0, 50)) out[core.mask(String(k))] = redact(v, k, depth + 1);
  if (entries.length > 50) out["…"] = `${entries.length - 50} more`;
  return out;
}

function canon(v, depth = 0) {
  if (v === null || v === undefined || typeof v === "boolean" || typeof v === "number" || typeof v === "string") return v ?? null;
  if (typeof v === "bigint") return String(v);
  if (ArrayBuffer.isView(v)) return { b: Buffer.from(v.buffer, v.byteOffset, v.byteLength).toString("base64") };
  if (typeof v !== "object" || depth > 8) return typeof v;
  if (Array.isArray(v) || v instanceof Set) return [...v].map((x) => canon(x, depth + 1));
  if (v instanceof Date) return v.toISOString();
  const entries = (v instanceof Map ? [...v] : Object.entries(v)).map(([k, x]) => [String(k), x]).sort((a, b) => (a[0] < b[0] ? -1 : 1));
  return Object.fromEntries(entries.map(([k, x]) => [k, canon(x, depth + 1)]));
}

function inputHash(args) {
  try {
    const list = [...args];
    while (list.length && list.at(-1) === undefined) list.pop();
    return createHash("sha256").update(JSON.stringify(list.map((a) => canon(a)))).digest("hex").slice(0, 16);
  } catch {
    return "?";
  }
}

// ---------------------------------------------------------------- recording

/** A hardened call has ended (called by the runtime). Returns the id of the incident it left, if any. */
function finish(ctx, outcome, error) {
  const broken = outcome === "raise" && error?.name !== "AbortError";
  const now = core.nowMs();
  const entry = { fid: ctx.fid, at: now, duration_s: round((now - ctx.t0) / 1000), outcome: broken ? "raise" : outcome };
  if (ctx.events.length) Object.assign(entry, { events: ctx.events, input_hash: inputHash(ctx.args), fault: ctx.gaveUp, seed: ctx.seed });
  const id = broken || ctx.gaveUp !== null ? dump(ctx, broken ? "raised" : "gave_up", outcome, error) : null;
  ring.push(entry);
  if (ring.length > RING) ring.shift();
  return id;
}

const round = (x) => Math.round(x * 1000) / 1000;

let quiet = false;

/** verify replays faults on purpose: nothing is written then. */
function mute(on = true) {
  quiet = on;
}

function folder() {
  const at = process.env.RAS_ANYTHING_INCIDENTS;
  if (at) return ["off", "0", "no"].includes(at.toLowerCase()) ? null : at;
  if (quiet) return null;
  for (let dir = process.cwd(); ;) {
    const ras = path.join(dir, ".ras");
    if (fs.existsSync(ras)) return fs.statSync(ras).isDirectory() ? path.join(ras, "incidents") : null;
    const up = path.dirname(dir);
    if (up === dir) return null;
    dir = up;
  }
}

/** Write out the call in progress now (from inside hardened code). Returns the incident id. */
function dumpIncident(reason = "manual") {
  const ctx = core.context();
  try {
    return ctx ? dump(ctx, reason, "running", null) : null;
  } catch {
    return null;
  }
}

function dump(ctx, trigger, outcome, error) {
  const at = folder();
  if (at === null) return null;
  let problem = null;
  if (error) {
    try {
      problem = require("./problem.cjs").problem(error);
    } catch {}
  }
  const fault = ctx.gaveUp ?? (error ? core.faultClass(error) : null);
  const kind = [ctx.fid, ctx.lastDependency, fault, trigger].join("\n");
  if ((written.get(kind) ?? 0) >= PER_KIND || [...written.values()].reduce((a, b) => a + b, 0) >= PER_PROCESS) return null;
  written.set(kind, (written.get(kind) ?? 0) + 1);
  const now = core.nowMs();
  const wall = new Date(Date.now());
  const before = ring.filter((e) => e.fid === ctx.fid).slice(-BEFORE).map(({ fid, at: ended, ...e }) =>
    ({ age_s: round((ctx.t0 - ended) / 1000 + e.duration_s), ...e }));
  const stamp = wall.toISOString().replace(/[-:]/g, "").slice(0, 15);
  const id = `inc-${stamp}-${ctx.fid.replace(/[^A-Za-z0-9_.-]/g, "_").slice(0, 60)}-${ctx.spanId.slice(0, 8)}`;
  const doc = {
    schema_version: SCHEMA_VERSION, kind: "incident", id, created: wall.toISOString().slice(0, 19) + "Z", lang: "node", service: core.SERVICE, pid: process.pid,
    function: ctx.fid, trigger, trace_id: ctx.traceId, span_id: ctx.spanId, parent_span_id: ctx.parent, seed: ctx.seed, started: ctx.ts / 1000,
    duration_s: round((now - ctx.t0) / 1000), inputs: redact([...ctx.args]), input_hash: inputHash(ctx.args), dependencies: ctx.deps,
    dependency: ctx.lastDependency, fault,
    outcome: { kind: outcome, error_type: error ? error.name ?? typeof error : null, message: error ? core.mask(String(error.message ?? error)).slice(0, 300) : null, problem },
    events: ctx.events, events_dropped: ctx.dropped, guards_at_start: ctx.health, before,
  };
  fs.mkdirSync(at, { recursive: true });
  const tmp = path.join(at, `.${id}.tmp`);
  fs.writeFileSync(tmp, JSON.stringify(doc, null, 1));
  fs.renameSync(tmp, path.join(at, `${id}.json`));
  for (const n of fs.readdirSync(at).filter((x) => x.startsWith("inc-") && x.endsWith(".json")).sort().slice(0, -KEEP_FILES)) fs.unlinkSync(path.join(at, n));
  core.emit("ras.incident", "ERROR", `incident ${id} written`, { "ras.incident.id": id, "ras.fault": fault, "ras.dependency": ctx.lastDependency });
  return id;
}

/** Forget what is in memory (tests). */
function reset() {
  ring.length = 0;
  written.clear();
}

module.exports = { finish, dumpIncident, redact, sensitive, inputHash, mute, reset };
