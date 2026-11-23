// Values and errors in cassettes (the tags are documented in docs/ARCHITECTURE.md). Nothing here imports
// or constructs what a file names: errors come from a fixed list of built-in classes.
"use strict";
const util = require("node:util");

const MAX_DEPTH = 60;
const MAX_NODES = 500_000;

class Unsupported extends Error {}

class BadCassette extends Error {}

/** Stands in for a recorded error whose class isn't one of Node's own. */
class RemoteError extends Error {
  constructor(type, msg) {
    super(msg);
    this.name = type;
  }
}

const ERRORS = { Error, TypeError, RangeError, SyntaxError, ReferenceError, EvalError, URIError, AggregateError };
const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const TYPED = new Map([Int8Array, Uint8Array, Uint8ClampedArray, Int16Array, Uint16Array, Int32Array, Uint32Array, Float32Array,
  Float64Array, BigInt64Array, BigUint64Array].map((c) => [c.name, c]));

class Codec {
  /** project: folder whose path is made relocatable inside strings. */
  constructor(project) {
    this.project = project && project.length > 3 ? project : null;
    this.re = this.project ? new RegExp(escape(this.project) + "(?![\\w.\\-])", "g") : null;
  }

  enc(v) {
    this.n = 0;
    return this._enc(v, 0);
  }

  can(v) {
    try {
      this.enc(v);
      return true;
    } catch {
      return false;
    }
  }

  _enc(v, d) {
    if (++this.n > MAX_NODES || d > MAX_DEPTH) throw new Unsupported("value is nested too deep (or refers to itself) or too large");
    switch (typeof v) {
      case "string": return this._str(v);
      case "boolean": return v;
      case "number":
        if (Number.isFinite(v)) return Object.is(v, -0) ? { $float: "-0" } : v;
        return { $float: Number.isNaN(v) ? "nan" : v > 0 ? "inf" : "-inf" };
      case "undefined": return { $undefined: 0 };
      case "bigint": return { $bigint: v.toString() };
      case "object": break;
      default: throw new Unsupported(`a ${typeof v}`);
    }
    if (v === null) return null;
    if (util.types.isProxy(v)) throw new Unsupported("a Proxy");
    if (Object.getOwnPropertySymbols(v).length) throw new Unsupported("an object with symbol keys");
    const proto = Object.getPrototypeOf(v);
    const plain = (n) => {
      if (Object.keys(v).length !== n) throw new Unsupported(`a ${v.constructor?.name} with extra properties`);
    };
    if (Array.isArray(v)) {
      if (proto !== Array.prototype) throw new Unsupported("a subclass of Array");
      if (Object.keys(v).length !== v.length) throw new Unsupported("an array with holes or extra properties");
      return v.map((x) => this._enc(x, d + 1));
    }
    if (proto === Object.prototype || proto === null) {
      const desc = Object.getOwnPropertyDescriptors(v);
      const keys = Object.keys(desc);
      for (const k of keys) {
        if (!desc[k].enumerable || !("value" in desc[k])) throw new Unsupported("an object with accessors or hidden properties");
      }
      if (proto === null) return { $nullproto: keys.map((k) => [this._str(k), this._enc(v[k], d + 1)]) };
      if (keys.every((k) => !k.startsWith("$") && k !== "__proto__" && typeof this._str(k) === "string")) {
        return Object.fromEntries(keys.map((k) => [k, this._enc(v[k], d + 1)]));
      }
      return { $dict: keys.map((k) => [this._str(k), this._enc(v[k], d + 1)]) };
    }
    if (Buffer.isBuffer(v)) return { $bytes: v.toString("base64") };
    if (ArrayBuffer.isView(v) && TYPED.get(v.constructor?.name)?.prototype === proto) {
      return { $typed: [v.constructor.name, Buffer.from(v.buffer, v.byteOffset, v.byteLength).toString("base64")] };
    }
    if (proto === Date.prototype) {
      plain(0);
      if (Number.isNaN(v.getTime())) throw new Unsupported("an invalid Date");
      return { $datetime: v.toISOString() };
    }
    if (proto === Map.prototype) {
      plain(0);
      return { $map: [...v].map(([k, x]) => [this._enc(k, d + 1), this._enc(x, d + 1)]) };
    }
    if (proto === Set.prototype) {
      plain(0);
      return { $set: [...v].map((x) => this._enc(x, d + 1)) };
    }
    if (proto === RegExp.prototype) {
      plain(0);
      return { $regexp: [v.source, v.flags] };
    }
    throw new Unsupported(v.constructor?.name ?? "an object");
  }

  _str(s) {
    if (!s.isWellFormed()) throw new Unsupported("text with lone surrogates");
    if (this.re && s.includes(this.project)) {
      let mark = "${PROJECT}";
      while (s.includes(mark)) mark = "$" + mark;
      const out = s.replace(this.re, () => mark);
      if (out !== s) return { $proj: [mark, out] };
    }
    return s;
  }

  dec(j) {
    if (j === null || typeof j !== "object") return j;
    if (Array.isArray(j)) return j.map((x) => this.dec(x));
    const keys = Object.keys(j);
    if (keys.length !== 1 || !keys[0].startsWith("$")) {
      const out = {};
      for (const k of keys) out[k] = this.dec(j[k]);
      return out;
    }
    const v = j[keys[0]];
    switch (keys[0]) {
      case "$undefined": return undefined;
      case "$float": return v === "-0" ? -0 : v === "nan" ? NaN : v === "inf" ? Infinity : -Infinity;
      case "$bigint": return BigInt(v);
      case "$bytes": return Buffer.from(v, "base64");
      case "$typed": {
        const C = TYPED.get(v[0]);
        if (!C) break;
        const b = Buffer.from(v[1], "base64");
        return new C(b.buffer.slice(b.byteOffset, b.byteOffset + b.length));
      }
      case "$datetime": return new Date(v);
      case "$map": return new Map(v.map(([k, x]) => [this.dec(k), this.dec(x)]));
      case "$set": return new Set(v.map((x) => this.dec(x)));
      case "$regexp": return new RegExp(v[0], v[1]);
      case "$proj": return this.project ? v[1].replaceAll(v[0], () => this.project) : v[1];
      case "$dict":
      case "$nullproto": {
        const out = keys[0] === "$dict" ? {} : Object.create(null);
        for (const [k, x] of v) Object.defineProperty(out, this.dec(k), { value: this.dec(x), writable: true, enumerable: true, configurable: true });
        return out;
      }
    }
    throw new BadCassette(`unknown value ${keys[0]}`);
  }
}

// ---------------------------------------------------------------- errors

/** {type, module, msg} plus code, errno and the other plain facts Node's errors carry. */
function dumpError(e, depth = 0) {
  if (!(e instanceof Error) && !util.types.isNativeError(e)) {
    let msg;
    try {
      msg = typeof e === "string" ? e : util.inspect(e, { depth: 1 });
    } catch {
      msg = String(typeof e);
    }
    return { type: typeof e, module: "thrown", msg: msg.slice(0, 2000) };
  }
  const d = { type: String(e.name || "Error"), module: "node", msg: String(e.message) };
  for (const k of ["code", "errno", "syscall", "address", "port", "path", "killed", "signal", "cmd", "status"]) {
    const v = e[k];
    if ((typeof v === "string" || typeof v === "number" || typeof v === "boolean") && !(k === "code" && e instanceof DOMException)) d[k] = v;
  }
  if (e.cause instanceof Error && depth < 4) d.cause = dumpError(e.cause, depth + 1);
  return d;
}

function buildError(d) {
  let e;
  const opts = d.cause ? { cause: buildError(d.cause) } : undefined;
  if (d.type === "TimeoutError" || d.type === "AbortError") e = new DOMException(String(d.msg), d.type);
  else if (d.type === "AggregateError") e = new AggregateError([], String(d.msg), opts);
  else if (Object.hasOwn(ERRORS, d.type)) e = new ERRORS[d.type](String(d.msg), opts);
  else e = new RemoteError(String(d.type), String(d.msg));
  if (!(e instanceof DOMException)) {
    for (const k of ["code", "errno", "syscall", "address", "port", "path", "killed", "signal", "cmd", "status"]) {
      if (k in d) e[k] = d[k];
    }
  }
  return e;
}

/** Is e of the recorded type (name of it or of a class it extends), with the recorded code? */
function matches(e, d) {
  if (d.module === "thrown") return typeof e === d.type && dumpError(e).msg === d.msg;
  if (!(e instanceof Error)) return false;
  if (d.code !== undefined && e.code !== d.code) return false;
  if (e.name === d.type) return true;
  for (let c = Object.getPrototypeOf(e); c; c = Object.getPrototypeOf(c)) {
    if (c.constructor?.name === d.type && d.type !== "Error") return true;
  }
  // a subclass of Error for a recorded plain Error: the same kind only if it carries the same message
  return d.type === "Error" && e.message === d.msg;
}

module.exports = { Codec, Unsupported, BadCassette, RemoteError, dumpError, buildError, matches };
