// flagd-compatible `.ras/flags.json` (or $RAS_ANYTHING_FLAGS). Hot-reloaded on mtime. Fail-safe.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const PATTERN_PREFIX = "ras.pattern.";
const ENABLE_PREFIX = "ras.enable.";
const SHADOW_PREFIX = "ras.shadow.";

function findPath() {
  if (process.env.RAS_ANYTHING_FLAGS) return process.env.RAS_ANYTHING_FLAGS;
  let dir = process.cwd();
  for (;;) {
    const ras = path.join(dir, ".ras");
    if (fs.existsSync(ras)) return path.join(ras, "flags.json");
    const parent = path.dirname(dir);
    if (parent === dir) return path.join(process.cwd(), ".ras", "flags.json");
    dir = parent;
  }
}

function empty() { return { flags: {} }; }

class Store {
  constructor() {
    this.doc = empty();
    this.lastGood = empty();
    this.mtimeMs = 0;
    this.version = 0;
    this.source = "default";
    this.path = findPath();
    this.loaded = false;
  }
  maybeReload() {
    let st;
    try { st = fs.statSync(this.path); } catch {
      if (!this.loaded) { this.loaded = true; this.source = "missing"; }
      else if (this.source !== "missing") {
        this.doc = empty(); this.lastGood = empty(); this.source = "missing"; this.version += 1; this.mtimeMs = 0;
      }
      return;
    }
    if (this.loaded && st.mtimeMs === this.mtimeMs) return;
    try {
      const doc = JSON.parse(fs.readFileSync(this.path, "utf8"));
      if (!doc || typeof doc !== "object" || (doc.flags && typeof doc.flags !== "object")) throw new Error("bad flags.json");
      doc.flags = doc.flags || {};
      this.doc = doc; this.lastGood = doc; this.source = "file";
    } catch {
      this.doc = this.lastGood; this.source = "last-known-good";
    }
    this.mtimeMs = st.mtimeMs; this.loaded = true; this.version += 1;
  }
  flag(key) {
    this.maybeReload();
    const f = this.doc.flags?.[key];
    return f && typeof f === "object" ? f : null;
  }
  evaluate(key, fallback) {
    try {
      const f = this.flag(key);
      if (!f) return fallback;
      if (String(f.state || "ENABLED").toUpperCase() === "DISABLED") {
        if (typeof fallback === "boolean") return false;
        if (typeof fallback === "number") return 0;
        return fallback;
      }
      const variants = f.variants || {};
      if (f.defaultVariant in variants) return variants[f.defaultVariant];
      const first = Object.keys(variants)[0];
      return first !== undefined ? variants[first] : fallback;
    } catch {
      return fallback;
    }
  }
}

let store = new Store();
function reset() { store = new Store(); }

function disabled() { return process.env.RAS_ANYTHING_DISABLE === "1"; }

function isOn(dependency, pattern, configured) {
  if (disabled()) return configured;
  const specific = `${PATTERN_PREFIX}${pattern}@${dependency}`;
  if (store.flag(specific)) return Boolean(store.evaluate(specific, configured));
  const general = PATTERN_PREFIX + pattern;
  if (store.flag(general)) return Boolean(store.evaluate(general, configured));
  return configured;
}

function enablePercent(fid) {
  if (disabled()) return 0;
  const v = store.evaluate(ENABLE_PREFIX + fid, 100);
  const n = Number(v);
  if (!Number.isFinite(n)) return v ? 100 : 0;
  return Math.max(0, Math.min(100, n));
}

function shadowOn(fid) {
  if (disabled()) return false;
  return Boolean(store.evaluate(SHADOW_PREFIX + fid, false));
}

function stickyBucket(key, buckets = 100) {
  const h = crypto.createHash("sha256").update(String(key)).digest();
  // first 8 bytes, big-endian, matching Python int.from_bytes(..., "big"); modulo in BigInt (Number is 53-bit).
  const n = (BigInt(h.readUInt32BE(0)) << 32n) + BigInt(h.readUInt32BE(4));
  return Number(n % BigInt(buckets));
}

function inCanary(fid, targetingKey) {
  const pct = enablePercent(fid);
  if (pct >= 100) return true;
  if (pct <= 0) return false;
  return stickyBucket(targetingKey || fid) < pct;
}

function targetingKey(args = []) {
  const a = args[0];
  if (typeof a === "string" || typeof a === "number") return String(a);
  if (a && typeof a === "object") {
    for (const k of ["idempotency_key", "targeting_key", "key", "order_id", "id"]) {
      if (a[k] != null && a[k] !== "") return String(a[k]);
    }
  }
  return null;
}

function route(fid, key) {
  try {
    if (disabled() || enablePercent(fid) <= 0) return "original";
    if (shadowOn(fid)) return "shadow";
    return inCanary(fid, key) ? "hardened" : "original";
  } catch {
    return "original";
  }
}

module.exports = { Store, store, reset, disabled, isOn, enablePercent, shadowOn, stickyBucket, inCanary, targetingKey, route, PATTERN_PREFIX, ENABLE_PREFIX, SHADOW_PREFIX };
