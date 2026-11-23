// Interception of the dependency boundary in the app's process:
//
//   globalThis.fetch · http.request/get · https.request/get ·
//   child_process.exec/execFile/spawn/execSync/execFileSync/spawnSync
//
// Every intercepted call becomes a request {kind, method, url, body, headers, argv, key} that the active
// handler performs with a `serve` function producing the real (or recorded) outcome. Outcomes are plain:
//
//   http:        {status, reason, headers: [[k, v]...], body: Buffer, url}
//   subprocess:  {returncode, signal?, stdout: Buffer, stderr: Buffer}
//   any:         {exc: {type, module, msg, ...}}
//
// Keys starting with "_" (the original error or Response) serve running code and are never recorded.
"use strict";
const { createHash } = require("node:crypto");
const cp = require("node:child_process");
const { errorMonitor } = require("node:events");
const http = require("node:http");
const https = require("node:https");
const net = require("node:net");
const path = require("node:path");
const util = require("node:util");
const { C, plan: makePlan, timeoutOf, ChaosHang, abortError } = require("./faults.cjs");
const { buildError, dumpError } = require("./values.cjs");
const { response, FakeClientRequest, FakeChild, DECODED } = require("./fakes.cjs");

const NONIDEMPOTENT = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const orig = {
  fetch: globalThis.fetch, httpRequest: http.request, httpsRequest: https.request, httpGet: http.get, httpsGet: https.get,
  exec: cp.exec, execFile: cp.execFile, spawn: cp.spawn, execSync: cp.execSync, execFileSync: cp.execFileSync, spawnSync: cp.spawnSync,
  fork: cp.fork, connect: net.Socket.prototype.connect,
};
const blame = new WeakMap(); // error handed to the app -> where its call was made

/** The code made a dependency request the recording never saw. Recorded as a miss even if it is caught. */
class ReplayMiss extends Error {
  constructor(msg) {
    super(msg);
    this.name = "ReplayMiss";
  }
}

// ---------------------------------------------------------------- requests

const sha = (body) => (body?.length ? createHash("sha256").update(body).digest("hex") : "");

/** The URL without its port, so the same logical request has the same key in every run. */
function logicalUrl(url) {
  try {
    const u = new URL(url);
    return `${u.protocol}//${u.hostname}${u.pathname}${u.search}`;
  } catch {
    return url;
  }
}

function portableArg(s, i, cwd) {
  if (i === 0 && (s === process.execPath || /^node(js)?(\.exe)?$/.test(path.basename(s)))) return "node";
  if (path.isAbsolute(s)) {
    const rel = path.relative(cwd, s);
    return rel && !rel.startsWith("..") && !path.isAbsolute(rel) ? rel.split(path.sep).join("/") : s;
  }
  return s;
}

function httpReq(kind, method, url, body, headers) {
  const bodySha = sha(body);
  return { kind, method, url, bodySha, headers, timeout: null, argv: null, nonidempotent: NONIDEMPOTENT.has(method),
    idem: headers["idempotency-key"] ?? null, key: `${method} ${logicalUrl(url)}${bodySha ? ` #${bodySha.slice(0, 12)}` : ""}` };
}

/** argv: the list of a file and its arguments, or a shell command line. */
function cpReq(argv, timeoutMs) {
  const cwd = process.cwd();
  const list = Array.isArray(argv) ? argv.map((a, i) => portableArg(String(a), i, cwd)) : null;
  return { kind: "subprocess", method: "RUN", url: "", bodySha: "", headers: {}, timeout: timeoutMs > 0 ? timeoutMs / 1000 : null,
    argv: list ?? argv, nonidempotent: false, idem: null, key: `run ${list ? list.join(" ") : argv}` };
}

const reqDict = (r) => ({ kind: r.kind, method: r.method, url: r.url, body_sha: r.bodySha, idempotency_key: r.idem, timeout: r.timeout,
  argv: r.argv, key: r.key });

const excOutcome = (e) => ({ exc: dumpError(e), _live: e });
const errorOf = (out) => out._live ?? buildError(out.exc);
const recorded = (out) => Object.fromEntries(Object.entries(out).filter(([k]) => !k.startsWith("_")));

/** Did the dependency fail? (exception or timeout, 5xx/429, a temp-failed or killed process) */
function isFailure(out) {
  if ("exc" in out) return true;
  if ("status" in out) return out.status >= 500 || out.status === 429;
  return out.returncode === 75 || Boolean(out.signal) || out.returncode < 0;
}

function blamed(e, where) {
  if (where && e !== null && typeof e === "object") blame.set(e, where);
  return e;
}

function truncate(out) {
  const half = (b) => b.subarray(0, Math.floor(b.length / 2));
  if (out.body?.length) return { ...out, body: half(out.body) };
  if (out.stdout?.length) return { ...out, stdout: half(out.stdout) };
  return out;
}

function refused(req) {
  const errno = (code, syscall, extra) => {
    const n = -(require("node:os").constants.errno[code] ?? 1);
    const addr = extra.address ? ` ${extra.address}:${extra.port}` : "";
    return Object.assign(new Error(`${syscall} ${code}${addr}`), { errno: n, code, syscall, ...extra });
  };
  if (req.kind === "subprocess") {
    const file = Array.isArray(req.argv) ? req.argv[0] : "/bin/sh";
    return errno("EAGAIN", `spawn ${file}`, { path: file, spawnargs: Array.isArray(req.argv) ? req.argv.slice(1) : ["-c", req.argv] });
  }
  let u;
  try {
    u = new URL(req.url);
  } catch {}
  const port = Number(u?.port) || (u?.protocol === "https:" ? 443 : 80);
  const e = errno("ECONNREFUSED", "connect", { address: u?.hostname || "127.0.0.1", port });
  return req.kind === "fetch" ? new TypeError("fetch failed", { cause: e }) : e;
}

function errorAnswer(req, status) {
  if (req.kind === "subprocess") return { returncode: 75, signal: null, stdout: Buffer.alloc(0), stderr: Buffer.from("temporarily unavailable (exit 75)\n") };
  const reason = status === 503 ? "Service Unavailable" : "Too Many Requests";
  const headers = [["Content-Type", "application/json"]];
  if (status === 429) headers.push(["Retry-After", C.retry_after]);
  return { status, reason, headers, body: Buffer.from(JSON.stringify({ error: reason.toLowerCase() })), url: req.url };
}

// ---------------------------------------------------------------- handlers

/** Looks on: `before(req)` for every call (scan). The calls themselves are untouched. */
class Watcher {
  constructor() {
    this.passive = true;
  }

  before() {}
}

/** Records: every call runs for real, `after(req, outcome, seconds)` sees it. */
class Recorder {
  after() {}

  async perform(req, serve) {
    const t0 = performance.now();
    const out = await serve({ whole: true });
    this.after(req, out, (performance.now() - t0) / 1000);
    return out;
  }

  performSync(req, serve) {
    const t0 = performance.now();
    const out = serve();
    this.after(req, out, (performance.now() - t0) / 1000);
    return out;
  }
}

/** Injects the faults a plan picks, on a live or virtual clock. Live, serve() makes the real call; in replay it
 *  returns the recorded outcome with `_dur`, which the virtual clock then adds.
 *
 *  ctl, how a call can be cut short: {timeoutS, cancelled, fire(), onCancel(fn), outcome(), where} */
class Faulting {
  constructor(plan, clock) {
    this.plan = makePlan(plan);
    this.clock = clock;
    this.reset();
  }

  reset() {
    this.counts = new Map();
    this.attempts = [];
  }

  onAttempt() {}

  open(req) {
    const n = this.counts.get(req.key) ?? 0;
    this.counts.set(req.key, n + 1);
    const rec = { key: req.key, n, fault: this.plan(req.key, n), reached: false, nonidempotent: req.nonidempotent, idem: req.idem,
      method: req.method, failed: true, outage: this.plan.outage(req.key, n) };
    this.attempts.push(rec);
    this.onAttempt(req, rec);
    this.clock.touch();
    return rec;
  }

  close(rec, out) {
    rec.failed = isFailure(out) || Boolean(rec.truncated);
    this.clock.touch();
    return out;
  }

  async perform(req, serve, ctl) {
    const rec = this.open(req);
    ctl.where ??= this.where?.();
    const cut = (out) => (ctl.cancelled ? ctl.outcome() : out);
    if (ctl.cancelled) return this.close(rec, ctl.outcome());
    const answer = async (extra, whole) => {
      if (this.clock.virtual) {
        const out = await serve({ whole });
        rec.reached = true;
        await this.clock.wait(extra + (out._dur ?? 0), ctl);
        return cut(out);
      }
      if (extra && ctl.timeoutS !== null && ctl.timeoutS < extra && req.nonidempotent) {
        await serve({ whole: true });
        rec.reached = true;
      }
      if (extra && (await this.clock.wait(extra, ctl))) return ctl.outcome();
      const out = await serve({ whole });
      rec.reached = true;
      return cut(out);
    };
    switch (rec.fault) {
      case null: return this.close(rec, await answer(0, false));
      case "slow": return this.close(rec, await answer(C.slow_s, false));
      case "truncated": {
        const whole = await answer(0, true);
        const out = truncate(whole);
        rec.truncated = out !== whole;
        return this.close(rec, out);
      }
      case "timeout":
        if (req.nonidempotent) { // the request went out; only the answer is lost
          await serve({ whole: true });
          rec.reached = true;
        }
        await this.clock.wait(Infinity, ctl);
        return this.close(rec, ctl.outcome());
      case "refused": return this.close(rec, excOutcome(refused(req)));
      default: return this.close(rec, errorAnswer(req, rec.fault === "http5xx" ? 503 : 429));
    }
  }

  /** For the blocking child_process calls; timedOut() is the outcome of their own timeout. */
  performSync(req, serve, timedOut) {
    const rec = this.open(req);
    const t = req.timeout;
    const wait = (s) => {
      if (t !== null && s > t) {
        this.clock.waitSync(t);
        return true;
      }
      this.clock.waitSync(s);
      return false;
    };
    const answer = (extra) => {
      if (this.clock.virtual) {
        const out = serve();
        rec.reached = true;
        return wait(extra + (out._dur ?? 0)) ? timedOut() : out;
      }
      if (extra && wait(extra)) return timedOut();
      const out = serve();
      rec.reached = true;
      return out;
    };
    switch (rec.fault) {
      case null: return this.close(rec, answer(0));
      case "slow": return this.close(rec, answer(C.slow_s));
      case "truncated": {
        const whole = answer(0);
        const out = truncate(whole);
        rec.truncated = out !== whole;
        return this.close(rec, out);
      }
      case "timeout":
        if (t === null) {
          this.clock.hangSync(this.where?.());
          throw new ChaosHang();
        }
        wait(Infinity);
        return this.close(rec, timedOut());
      case "refused": return this.close(rec, excOutcome(refused(req)));
      default: return this.close(rec, errorAnswer(req, rec.fault === "http5xx" ? 503 : 429));
    }
  }
}

const earliest = (s, known) => (known && (s === null || known.s < s) ? known.s : s);

let handler = new Watcher();
let busy = false; // inside an original that calls another patched function

function quietly(fn) {
  busy = true;
  try {
    return fn();
  } finally {
    busy = false;
  }
}

// ---------------------------------------------------------------- fetch

function bodyBytes(body) {
  if (body === undefined || body === null) return null;
  if (typeof body === "string") return Buffer.from(body);
  if (body instanceof URLSearchParams) return Buffer.from(body.toString());
  if (body instanceof ArrayBuffer) return Buffer.from(body);
  if (ArrayBuffer.isView(body)) return Buffer.from(body.buffer, body.byteOffset, body.byteLength);
  return undefined; // needs reading (Blob, FormData) or can't be (a stream)
}

async function readBody(body) {
  if (body instanceof Blob) return Buffer.from(await body.arrayBuffer());
  if (body instanceof FormData) {
    const parts = [];
    for (const [k, v] of body) parts.push(k, typeof v === "string" ? v : Buffer.from(await v.arrayBuffer()).toString("base64"));
    return Buffer.from(parts.join("\n"));
  }
  return Buffer.from("[stream]"); // reading it would use it up
}

class SignalCtl {
  constructor(signal) {
    this.signal = signal ?? null;
    this.known = timeoutOf(signal);
    this.timeoutS = this.known?.s ?? null;
    this.where = null;
  }

  get cancelled() { return Boolean(this.signal?.aborted); }
  fire() { this.known?.fire(); }
  onCancel(fn) {
    if (!this.signal) return;
    if (this.signal.aborted) fn();
    else this.signal.addEventListener("abort", fn, { once: true });
  }
  outcome() { return excOutcome(this.signal.reason); }
}

function fetchPatch(input, init) {
  if (busy || handler.passive) {
    if (!busy) handler.before({ kind: "fetch" });
    return orig.fetch.call(globalThis, input, init);
  }
  return doFetch(input, init);
}

async function doFetch(input, init) {
  const isReq = typeof input === "object" && input !== null && !(input instanceof URL) && typeof input.url === "string";
  const method = String(init?.method ?? (isReq ? input.method : "GET")).toUpperCase();
  const url = new URL(isReq ? input.url : String(input)).href; // (throws TypeError like fetch does)
  let body = init?.body !== undefined ? init.body : null;
  let bytes = bodyBytes(body);
  if (bytes === undefined) bytes = await readBody(body);
  if (body === null && isReq && input.body) bytes = Buffer.from(await input.clone().arrayBuffer());
  const headers = Object.fromEntries(new Headers(init?.headers ?? (isReq ? input.headers : undefined)));
  const where = handler.where?.();
  const ctl = new SignalCtl(init?.signal ?? (isReq ? input.signal : null));
  const req = httpReq("fetch", method, url, bytes, headers);
  req.timeout = ctl.timeoutS;
  const serve = async ({ whole }) => {
    try {
      const res = await orig.fetch.call(globalThis, input, init);
      if (!whole) return { status: res.status, _response: res };
      return { status: res.status, reason: res.statusText, headers: [...res.headers].filter(([k]) => !DECODED.has(k)),
        body: Buffer.from(await res.arrayBuffer()), url: res.url };
    } catch (e) {
      return excOutcome(e);
    }
  };
  const out = await handler.perform(req, serve, ctl);
  if ("exc" in out) throw blamed(errorOf(out), where);
  return out._response ?? response(out);
}

// ---------------------------------------------------------------- http / https

function httpArgs(protocol, args) {
  let [input, options, cb] = args;
  let base;
  if (typeof input === "string" || input instanceof URL) {
    base = new URL(input);
    if (typeof options === "function") [options, cb] = [{}, options];
    options = { ...options };
  } else {
    if (typeof options === "function") cb = options;
    options = { ...input };
    base = new URL(`${options.protocol ?? protocol}//localhost`);
  }
  const host = options.hostname ?? options.host;
  if (host) base.hostname = String(host).includes(":") && !String(host).startsWith("[") ? `[${host}]` : String(host).replace(/:\d+$/, "");
  if (options.port) base.port = String(options.port);
  if (options.path !== undefined) {
    const [p, ...q] = String(options.path).split("?");
    base.pathname = p;
    base.search = q.length ? `?${q.join("?")}` : "";
  }
  if (options.headers instanceof Headers) options.headers = Object.fromEntries(options.headers);
  return { url: base, options, cb };
}

function httpPatch(protocol, name, getter) {
  const secure = protocol === "https:";
  const original = secure ? orig.httpsRequest : orig.httpRequest;
  return function request(...args) {
    if (busy || handler.passive) {
      if (!busy) handler.before({ kind: "http" });
      return quietly(() => original.apply(this, args));
    }
    const { url, options, cb } = httpArgs(protocol, args);
    const where = handler.where?.();
    const req = new FakeClientRequest(url, options, send);
    if (cb) req.once("response", cb);
    if (getter) req.end();
    return req;

    function send(fake, body) {
      const headers = Object.fromEntries(Object.entries(fake.getHeaders()).map(([k, v]) => [k, String(v)]));
      const r = httpReq("http", fake.method, url.href, body, headers);
      const known = timeoutOf(options.signal);
      const ctl = {
        where: null, timer: null,
        get timeoutS() { return earliest(fake.timeoutMs === null ? null : fake.timeoutMs / 1000, known); },
        get cancelled() { return fake.destroyed; },
        fire() {
          if (known && (fake.timeoutMs === null || known.s * 1000 < fake.timeoutMs)) return known.fire();
          clearTimeout(ctl.timer);
          fake.emit("timeout");
        },
        onCancel: (fn) => fake.onCancel(fn),
        outcome: () => excOutcome(Object.assign(new Error("socket hang up"), { code: "ECONNRESET" })),
      };
      r.timeout = ctl.timeoutS;
      const arm = () => {
        clearTimeout(ctl.timer);
        if (fake.timeoutMs !== null) ctl.timer = setTimeout(() => ctl.cancelled || fake.emit("timeout"), fake.timeoutMs);
      };
      arm();
      fake.onTimeoutChange = arm;
      handler.perform(r, ({ whole }) => liveHttp(original, url, options, fake, body), ctl).then((out) => {
        clearTimeout(ctl.timer);
        if ("exc" in out) fake.fail(blamed(errorOf(out), where));
        else fake.answer(out);
      }, (e) => fake.fail(e));
    }
  };
}

/** The real request behind a fake one: its whole answer, or its error. */
function liveHttp(original, url, options, fake, body) {
  return new Promise((resolve) => {
    const { timeout, signal, ...rest } = options;
    const opts = { ...rest, protocol: url.protocol, hostname: url.hostname.replace(/^\[|\]$/g, ""), port: url.port || undefined,
      path: url.pathname + url.search, method: fake.method, headers: Object.fromEntries(fake._headers.values()) };
    delete opts.host;
    delete opts.auth;
    const req = quietly(() => original(opts, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => {
        const headers = [];
        for (let i = 0; i < res.rawHeaders.length; i += 2) headers.push([res.rawHeaders[i], res.rawHeaders[i + 1]]);
        const whole = Buffer.concat(chunks);
        resolve({ status: res.statusCode, reason: res.statusMessage,
          headers: headers.filter(([k]) => k.toLowerCase() !== "transfer-encoding"), body: whole, url: url.href });
      });
      res.on("error", (e) => resolve(excOutcome(e)));
    }));
    req.on("error", (e) => resolve(excOutcome(e)));
    fake.onCancel(() => req.destroy());
    req.end(body.length ? body : undefined);
  });
}

// ---------------------------------------------------------------- child processes

const toText = (buf, encoding) => (buf === null || buf === undefined ? buf : encoding && encoding !== "buffer" ? buf.toString(encoding) : buf);

/** Runs `done(out)` with the outcome of a live child, without disturbing what the app does with it. */
function observe(child, done) {
  const tap = (s) => {
    const chunks = [];
    if (s) {
      const push = s.push;
      s.push = function (c, ...a) {
        if (c) chunks.push(Buffer.isBuffer(c) ? c : Buffer.from(c));
        return push.call(this, c, ...a);
      };
    }
    return chunks;
  };
  const [o, e] = [tap(child.stdout), tap(child.stderr)];
  let failed = null;
  child.on(errorMonitor, (err) => { // (an "error" listener would keep an unhandled one from crashing the app)
    failed ??= err;
  });
  child.once("close", (code, signal) => done(failed && child.pid === undefined ? excOutcome(failed)
    : { returncode: code, signal: signal ?? null, stdout: Buffer.concat(o), stderr: Buffer.concat(e) }));
}

function spawnPatch(file, args, options) {
  if (busy || handler.passive) {
    if (!busy) handler.before({ kind: "subprocess" });
    return quietly(() => orig.spawn.call(this, file, args, options));
  }
  if (!Array.isArray(args)) [args, options] = [[], args];
  options ??= {};
  const argv = options.shell ? [file, ...args].join(" ") : [file, ...args];
  return launch(file, args ?? [], options, cpReq(argv, options.timeout), () => orig.spawn.call(this, file, args ?? [], options));
}

/** A child process for an intercepted call: the live one when nothing is injected, else a stand-in. */
function launch(file, args, options, req, live) {
  if (handler.onLive) { // recording: the live child, observed
    const child = quietly(live);
    const t0 = performance.now();
    const after = handler.onLive(req);
    observe(child, (out) => after(out, (performance.now() - t0) / 1000));
    return child;
  }
  if (!handler.clock.virtual) {
    const n = handler.counts.get(req.key) ?? 0;
    if (handler.plan(req.key, n) === null) {
      const rec = handler.open(req);
      const child = quietly(live);
      rec.reached = true;
      observe(child, (out) => handler.close(rec, out));
      return child;
    }
  }
  const child = new FakeChild(file, args, options.stdio);
  child.pid = 4_000_000;
  const { signal } = options;
  const timedOut = () => child.kill(options.killSignal ?? "SIGTERM");
  const known = timeoutOf(signal);
  const ctl = {
    where: null, timeoutS: earliest(req.timeout, known),
    get cancelled() { return child.done; },
    fire: () => (known && (req.timeout === null || known.s < req.timeout) ? known.fire() : timedOut()),
    onCancel: (fn) => child.onCancel(fn),
    outcome: () => ({ returncode: null, signal: child.signalCode ?? "SIGTERM", stdout: Buffer.alloc(0), stderr: Buffer.alloc(0) }),
  };
  let timer = null;
  if (handler.clock.virtual && req.timeout !== null) timer = setTimeout(timedOut, req.timeout * 1000);
  if (signal) {
    const abort = () => {
      if (child.done) return;
      child.fail(abortError(signal));
    };
    if (signal.aborted) process.nextTick(abort);
    else signal.addEventListener("abort", abort, { once: true });
  }
  const serve = () => new Promise((resolve) => observe(quietly(live), resolve));
  process.nextTick(() => child.done || child.emit("spawn"));
  handler.perform(req, serve, ctl).then((out) => {
    clearTimeout(timer);
    if ("exc" in out) {
      child.pid = undefined;
      child.fail(errorOf(out));
    } else child.end(out);
  }, (e) => child.fail(e));
  return child;
}

function execFilePatch(file, args, options, callback) {
  if (busy || handler.passive) {
    if (!busy) handler.before({ kind: "subprocess" });
    return quietly(() => orig.execFile.apply(this, arguments));
  }
  if (typeof args === "function") [args, options, callback] = [[], {}, args];
  else if (!Array.isArray(args)) [args, options, callback] = [[], args, options];
  if (typeof options === "function") [options, callback] = [{}, options];
  return execLike(file, args, options ?? {}, options?.shell ? [file, ...args].join(" ") : [file, ...args], callback);
}

function execPatch(command, options, callback) {
  if (busy || handler.passive) {
    if (!busy) handler.before({ kind: "subprocess" });
    return quietly(() => orig.exec.apply(this, arguments));
  }
  if (typeof options === "function") [options, callback] = [{}, options];
  return execLike("/bin/sh", ["-c", command], options ?? {}, command, callback);
}

/** execFile/exec: a child plus the (err, stdout, stderr) callback they promise. */
function execLike(file, args, options, argv, callback) {
  const { encoding = "utf8", maxBuffer = 1024 * 1024 } = options;
  const where = handler.where?.();
  const { shell } = options;
  const child = launch(file, args, { ...options, stdio: undefined }, cpReq(argv, options.timeout), () =>
    (Array.isArray(argv) ? orig.execFile : orig.spawn).call(cp, file, args, { ...options, encoding: "buffer", maxBuffer: Infinity, stdio: undefined, shell: Array.isArray(argv) ? shell : false }));
  const out = [];
  const err = [];
  child.stdout?.on("data", (c) => out.push(Buffer.from(c)));
  child.stderr?.on("data", (c) => err.push(Buffer.from(c)));
  let failed = null;
  let over = false;
  child.on("error", (e) => {
    failed ??= e;
  });
  child.once("close", (code, signal) => {
    if (over) return;
    over = true;
    const [so, se] = [Buffer.concat(out), Buffer.concat(err)];
    const cmd = Array.isArray(argv) ? argv.join(" ") : argv;
    let e = failed;
    if (!e && so.length > maxBuffer) e = Object.assign(new RangeError("stdout maxBuffer length exceeded"), { code: "ERR_CHILD_PROCESS_STDIO_MAXBUFFER" });
    if (!e && (code !== 0 || signal !== null)) {
      e = Object.assign(new Error(`Command failed: ${cmd}\n${se}`), { code: code ?? null, killed: child.killed || false, signal: signal ?? null });
    }
    if (e) blamed(e, where).cmd = cmd;
    callback?.(e ?? null, toText(so, encoding), toText(se, encoding));
  });
  return child;
}

function promisified(fn) {
  return (...args) => {
    let child;
    const p = new Promise((resolve, reject) => {
      child = fn(...args, (err, stdout, stderr) => {
        if (err) reject(Object.assign(err, { stdout, stderr }));
        else resolve({ stdout, stderr });
      });
    });
    p.child = child;
    return p;
  };
}

/** spawnSync's result for an intercepted call; live() makes the real one. */
function runSync(argv, options, live) {
  const req = cpReq(argv, options.timeout);
  const serve = () => {
    const r = quietly(live);
    return r.error ? { ...excOutcome(r.error), _signal: r.signal }
      : { returncode: r.status, signal: r.signal ?? null, stdout: r.stdout, stderr: r.stderr };
  };
  const timedOut = () => {
    const file = Array.isArray(argv) ? argv[0] : "/bin/sh";
    const e = Object.assign(new Error(`spawnSync ${file} ETIMEDOUT`), { errno: -110, code: "ETIMEDOUT", syscall: `spawnSync ${file}`, path: file,
      spawnargs: Array.isArray(argv) ? argv.slice(1) : ["-c", argv] });
    return { exc: dumpError(e), _live: e, _signal: options.killSignal ?? "SIGTERM" };
  };
  const out = handler.performSync(req, serve, timedOut);
  if ("exc" in out) {
    const e = errorOf(out);
    if (e.code === "EAGAIN" || e.syscall?.startsWith("spawn ")) {
      e.syscall = e.syscall?.replace(/^spawn /, "spawnSync ");
      e.message = e.message.replace(/^spawn /, "spawnSync ");
    }
    const empty = out._signal || e.code === "ETIMEDOUT" ? Buffer.alloc(0) : null;
    return { pid: 0, output: empty && [null, empty, empty], stdout: empty, stderr: empty, status: null,
      signal: out._signal ?? (e.code === "ETIMEDOUT" ? "SIGTERM" : null), error: e };
  }
  return { pid: 4_000_000, output: [null, out.stdout, out.stderr], stdout: out.stdout, stderr: out.stderr, status: out.returncode, signal: out.signal ?? null };
}

const bufferOptions = (o) => ({ ...o, encoding: "buffer" });

function spawnSyncPatch(file, args, options) {
  if (busy || handler.passive) {
    if (!busy) handler.before({ kind: "subprocess" });
    return quietly(() => orig.spawnSync.call(this, file, args, options));
  }
  if (!Array.isArray(args)) [args, options] = [[], args];
  options ??= {};
  const r = runSync(options.shell ? [file, ...args].join(" ") : [file, ...args], options, () => orig.spawnSync(file, args, bufferOptions(options)));
  for (const k of ["stdout", "stderr"]) r[k] = toText(r[k], options.encoding);
  r.output = r.output && [null, r.stdout, r.stderr];
  return r;
}

/** execSync/execFileSync: stdout, or the error they throw. */
function syncOutput(r, options, cmd) {
  const inherit = options.stdio === undefined;
  if (inherit && r.stderr?.length) process.stderr.write(r.stderr);
  const stdout = toText(r.stdout, options.encoding);
  if (r.error || r.status !== 0) {
    const e = r.error ?? new Error(`Command failed: ${cmd}${r.stderr?.length ? `\n${r.stderr}` : ""}`);
    const stderr = toText(r.stderr, options.encoding);
    throw Object.assign(e, { pid: r.pid, output: r.output && [null, stdout, stderr], stdout, stderr, status: r.status, signal: r.signal });
  }
  return stdout;
}

function execFileSyncPatch(file, args, options) {
  if (busy || handler.passive) {
    if (!busy) handler.before({ kind: "subprocess" });
    return quietly(() => orig.execFileSync.call(this, file, args, options));
  }
  if (!Array.isArray(args)) [args, options] = [[], args];
  options ??= {};
  const argv = [file, ...args];
  const stdio = options.stdio ?? "pipe";
  return syncOutput(runSync(options.shell ? argv.join(" ") : argv, options, () => orig.spawnSync(file, args, { ...bufferOptions(options), stdio })), options, argv.join(" "));
}

function execSyncPatch(command, options) {
  if (busy || handler.passive) {
    if (!busy) handler.before({ kind: "subprocess" });
    return quietly(() => orig.execSync.call(this, command, options));
  }
  options ??= {};
  const stdio = options.stdio ?? "pipe";
  const shell = typeof options.shell === "string" ? options.shell : true;
  return syncOutput(runSync(command, options, () => orig.spawnSync(command, { ...bufferOptions(options), stdio, shell })), options, command);
}

// ---------------------------------------------------------------- install

let installed = false;

/** Route every boundary call through `h` (patches are installed once per process). */
function install(h) {
  handler = h;
  if (installed) return;
  installed = true;
  if (orig.fetch) globalThis.fetch = fetchPatch;
  http.request = httpPatch("http:", "request", false);
  http.get = httpPatch("http:", "get", true);
  https.request = httpPatch("https:", "request", false);
  https.get = httpPatch("https:", "get", true);
  const passiveGet = (patched, original) => function get(...args) {
    if (busy || handler.passive) {
      if (!busy) handler.before({ kind: "http" });
      return quietly(() => original.apply(this, args));
    }
    return patched.apply(this, args);
  };
  http.get = passiveGet(http.get, orig.httpGet);
  https.get = passiveGet(https.get, orig.httpsGet);
  Object.assign(cp, { exec: execPatch, execFile: execFilePatch, spawn: spawnPatch, execSync: execSyncPatch,
    execFileSync: execFileSyncPatch, spawnSync: spawnSyncPatch });
  Object.defineProperty(cp.exec, util.promisify.custom, { value: promisified(execPatch) });
  Object.defineProperty(cp.execFile, util.promisify.custom, { value: promisified(execFilePatch) });
  require("node:module").syncBuiltinESMExports();
}

/** Replay: nothing reaches the network by other means either. */
function seal(miss) {
  net.Socket.prototype.connect = function (...args) {
    const o = Array.isArray(args[0]) ? args[0][0] : args[0];
    const where = typeof o === "object" && o ? o.path ?? `${o.host ?? "localhost"}:${o.port}` : `${args[1] ?? "localhost"}:${o}`;
    miss(`raw connection to ${where}`);
  };
  cp.fork = (file) => miss(`fork ${file}`);
  require("node:module").syncBuiltinESMExports();
}

module.exports = { install, seal, blame, Watcher, Recorder, Faulting, ReplayMiss, reqDict, recorded, excOutcome, isFailure, quietly };
