// What intercepted calls hand back to the app instead of live objects: fetch Responses, http requests and
// responses, child processes.
"use strict";
const { Duplex, Readable, Writable } = require("node:stream");
const { EventEmitter } = require("node:events");
const http = require("node:http");
const os = require("node:os");

// ---------------------------------------------------------------- fetch

const DECODED = new Set(["content-encoding", "content-length", "transfer-encoding"]);

/** A fetch Response with a complete, already decoded body. */
function response(out) {
  const body = [101, 204, 205, 304].includes(out.status) ? null : out.body ?? Buffer.alloc(0);
  const headers = new Headers();
  for (const [k, v] of out.headers ?? []) {
    try {
      headers.append(k, v);
    } catch {} // not a valid header for fetch
  }
  const r = new Response(body, { status: out.status, statusText: out.reason ?? "", headers });
  for (const [k, v] of [["url", out.url ?? ""], ["type", "basic"]]) Object.defineProperty(r, k, { value: v, configurable: true });
  return r;
}

// ---------------------------------------------------------------- http

class FakeSocket extends Duplex {
  constructor() {
    super({ emitClose: false });
    Object.assign(this, { remoteAddress: "127.0.0.1", remotePort: 0, encrypted: false, connecting: false });
  }

  _read() {}
  _write(c, e, cb) { cb(); }
  setTimeout() { return this; }
  setNoDelay() { return this; }
  setKeepAlive() { return this; }
  ref() { return this; }
  unref() { return this; }
  address() { return {}; }
}

/** http.IncomingMessage over a complete body. */
class FakeIncoming extends Readable {
  constructor(out, req) {
    super();
    const raw = (out.headers ?? []).map(([k, v]) => [String(k), String(v)]);
    Object.assign(this, {
      statusCode: out.status, statusMessage: out.reason ?? "", httpVersion: "1.1", httpVersionMajor: 1, httpVersionMinor: 1, url: "",
      method: null, complete: false, aborted: false, req, socket: req.socket, connection: req.socket, rawHeaders: raw.flat(), rawTrailers: [],
      trailers: {}, headers: headerObject(raw), headersDistinct: distinct(raw), _body: out.body ?? Buffer.alloc(0),
    });
  }

  _read() {
    if (this._body?.length) this.push(this._body);
    this._body = null;
    this.complete = true;
    this.push(null);
  }

  setTimeout(ms, cb) { return this; }
}

function headerObject(pairs) {
  const out = {};
  for (const [name, v] of pairs) {
    const k = name.toLowerCase();
    if (k === "set-cookie") (out[k] ??= []).push(v);
    else if (k in out) out[k] += (k === "cookie" ? "; " : ", ") + v;
    else out[k] = v;
  }
  return out;
}

function distinct(pairs) {
  const out = Object.create(null);
  for (const [k, v] of pairs) (out[k.toLowerCase()] ??= []).push(v);
  return out;
}

/** http.ClientRequest that collects the request and hands it to `send(req) -> outcome` once it is complete. */
class FakeClientRequest extends Writable {
  constructor(url, options, send) {
    super({ autoDestroy: false, emitClose: false });
    Object.assign(this, {
      method: String(options.method ?? "GET").toUpperCase(), path: url.pathname + url.search, host: url.hostname, protocol: url.protocol,
      href: url.href, agent: options.agent, aborted: false, reusedSocket: false, headersSent: false, maxHeadersCount: null, res: null,
      socket: new FakeSocket(), timeoutMs: options.timeout > 0 ? Number(options.timeout) : null, shouldKeepAlive: false,
    });
    this.connection = this.socket;
    this._chunks = [];
    this._headers = new Map();
    this._cancels = [];
    const h = options.headers;
    if (Array.isArray(h)) {
      for (let i = 0; i < h.length; i += Array.isArray(h[i]) ? 1 : 2) Array.isArray(h[i]) ? this.setHeader(h[i][0], h[i][1]) : this.setHeader(h[i], h[i + 1]);
    } else for (const [k, v] of Object.entries(h ?? {})) if (v !== undefined) this.setHeader(k, v);
    if (options.auth && !this.hasHeader("authorization")) this.setHeader("Authorization", "Basic " + Buffer.from(options.auth).toString("base64"));
    if (!this.hasHeader("host")) this.setHeader("Host", url.host);
    const { signal } = options;
    if (signal) {
      const abort = () => this.destroy(require("./faults.cjs").abortError(signal));
      if (signal.aborted) process.nextTick(abort);
      else signal.addEventListener("abort", abort, { once: true });
    }
    this._send = send;
    process.nextTick(() => {
      if (!this.destroyed) this.emit("socket", this.socket);
    });
  }

  setHeader(k, v) {
    http.validateHeaderName?.(String(k));
    this._headers.set(String(k).toLowerCase(), [String(k), v]);
    return this;
  }

  getHeader(k) { return this._headers.get(String(k).toLowerCase())?.[1]; }
  hasHeader(k) { return this._headers.has(String(k).toLowerCase()); }
  removeHeader(k) { this._headers.delete(String(k).toLowerCase()); }
  getHeaderNames() { return [...this._headers.keys()]; }
  getRawHeaderNames() { return [...this._headers.values()].map((x) => x[0]); }
  getHeaders() { return Object.assign(Object.create(null), Object.fromEntries([...this._headers].map(([k, x]) => [k, x[1]]))); }
  appendHeader(k, v) { return this.setHeader(k, [].concat(this.getHeader(k) ?? [], v)); }
  setHeaders(hs) {
    for (const [k, v] of hs) this.setHeader(k, v);
    return this;
  }
  flushHeaders() { this.headersSent = true; }
  setNoDelay() {}
  setSocketKeepAlive() {}
  addTrailers() {}
  cork() {}
  uncork() {}
  get finished() { return this.writableEnded; }

  setTimeout(ms, cb) {
    this.timeoutMs = ms > 0 ? ms : null;
    if (cb) this.once("timeout", cb);
    this.onTimeoutChange?.();
    return this;
  }

  _write(chunk, enc, cb) {
    this.headersSent = true;
    this._chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, enc === "buffer" ? undefined : enc));
    cb();
  }

  _final(cb) {
    this.headersSent = true;
    cb();
    if (!this._headers.has("content-length") && this._chunks.length) this.setHeader("Content-Length", this._chunks.reduce((n, c) => n + c.length, 0));
    this._send(this, Buffer.concat(this._chunks));
  }

  onCancel(fn) {
    if (this.destroyed) fn();
    else this._cancels.push(fn);
  }

  _destroy(err, cb) {
    if (!err && !this.res) err = Object.assign(new Error("socket hang up"), { code: "ECONNRESET" });
    for (const fn of this._cancels.splice(0)) fn();
    if (this.res && !this.res.complete) this.res.destroy(err ?? Object.assign(new Error("aborted"), { code: "ECONNRESET" }));
    cb(this.res ? null : err);
    process.nextTick(() => this.emit("close"));
  }

  abort() {
    if (this.aborted) return;
    this.aborted = true;
    process.nextTick(() => this.emit("abort"));
    this.destroy();
  }

  answer(out) {
    if (this.destroyed) return;
    const res = (this.res = new FakeIncoming(out, this));
    res.once("close", () => this.emit("close"));
    if (!this.emit("response", res)) res.resume();
  }

  fail(err) {
    if (this.destroyed) return;
    this.destroy(err);
  }
}

// ---------------------------------------------------------------- child processes

const signalNumber = (s) => os.constants.signals[s] ?? 15;

class FakeChild extends EventEmitter {
  constructor(file, args, stdio) {
    super();
    const pipe = (i) => {
      const s = Array.isArray(stdio) ? stdio[i] : stdio;
      return s === undefined || s === null || s === "pipe" || s === "overlapped";
    };
    Object.assign(this, { pid: undefined, exitCode: null, signalCode: null, killed: false, connected: false, spawnfile: file,
      spawnargs: [file, ...args], _cancels: [] });
    this.stdin = pipe(0) ? new Writable({ write: (c, e, cb) => cb() }) : null;
    this.stdout = pipe(1) ? new Readable({ read() {} }) : null;
    this.stderr = pipe(2) ? new Readable({ read() {} }) : null;
    this.stdio = [this.stdin, this.stdout, this.stderr];
    this.done = false;
  }

  onCancel(fn) {
    if (this.done) fn();
    else this._cancels.push(fn);
  }

  kill(sig = "SIGTERM") {
    if (this.done) return false;
    this.killed = true;
    this.end({ returncode: null, signal: typeof sig === "number" ? "SIGTERM" : sig, stdout: Buffer.alloc(0), stderr: Buffer.alloc(0) });
    return true;
  }

  ref() {}
  unref() {}
  disconnect() {}
  send() { return false; }
  [Symbol.dispose]() { this.kill(); }

  /** The process ran: deliver its output and exit. */
  end(out) {
    if (this.done) return;
    this.done = true;
    for (const fn of this._cancels.splice(0)) fn();
    this.exitCode = out.returncode ?? null;
    this.signalCode = out.signal ?? null;
    let open = 0;
    const close = () => {
      if (--open <= 0) this.emit("close", this.exitCode, this.signalCode);
    };
    for (const [s, data] of [[this.stdout, out.stdout], [this.stderr, out.stderr]]) {
      if (!s) continue;
      open++;
      s.once("close", close);
      if (data?.length) s.push(data);
      s.push(null);
    }
    this.stdin?.destroy();
    process.nextTick(() => {
      this.emit("exit", this.exitCode, this.signalCode);
      for (const s of [this.stdout, this.stderr]) if (s && s.listenerCount("data") + s.listenerCount("readable") === 0 && !s.readableFlowing) s.resume();
      if (open === 0) this.emit("close", this.exitCode, this.signalCode);
    });
  }

  /** The process couldn't be started. */
  fail(err) {
    if (this.done) return;
    this.done = true;
    for (const fn of this._cancels.splice(0)) fn();
    process.nextTick(() => {
      this.emit("error", err);
      for (const s of this.stdio) s?.destroy();
      this.emit("close", err.errno ?? -1, null);
    });
  }
}

module.exports = { response, FakeClientRequest, FakeIncoming, FakeChild, headerObject, signalNumber, DECODED };
