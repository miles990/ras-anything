// Runs inside the user's Node process (node --require .../agent.cjs). Not for direct use.
//
// Modes (from the JSON in RAS_ANYTHING_CFG; the wire format is docs/ARCHITECTURE.md):
//   scan    count calls of the project's functions, attribute boundary calls to the innermost exported one
//   chaos   inject seeded faults at the boundary (live), attribute each fault to a function
//   record  record up to N calls of one function: args, result/error and its boundary I/O
//   bench   like chaos (or clean when seed is null) plus per-function stats for the hardened ids
// Results go to cfg.out as JSON when the process exits.
"use strict";
const { isMainThread } = require("node:worker_threads");

const raw = process.env.RAS_ANYTHING_CFG;
delete process.env.RAS_ANYTHING_CFG;
if (raw && isMainThread) {
  try {
    start(JSON.parse(raw));
  } catch (e) { // never break the app
    process.stderr.write(`ras-anything: can't watch this process: ${e?.stack ?? e}\n`);
  }
}

function start(cfg) {
  const fs = require("node:fs");
  const path = require("node:path");
  const { AsyncLocalStorage } = require("node:async_hooks");
  const { fileURLToPath } = require("node:url");
  const boundary = require("./boundary.cjs");
  const faults = require("./faults.cjs");
  const { Codec, dumpError } = require("./values.cjs");
  const hooks = require("./hooks.cjs");

  dropExecArg();
  const now = faults.real.now;
  const spec = JSON.parse(fs.readFileSync(cfg.node_spec, "utf8"));
  const codec = new Codec(cfg.project ?? cfg.root);
  const frames = new AsyncLocalStorage();
  const stats = new Map(); // function -> what was seen of it
  const out = { protocol: 1, mode: cfg.mode, runtime: { name: "Node.js", version: process.versions.node }, executable: process.execPath,
    uncaught: null, boundary_total: 0, hangs: 0 };
  let handler;
  let finished = false;

  // ---------------------------------------------------------------- attribution

  function note(fn) {
    let st = stats.get(fn);
    if (!st) {
      st = { calls: 0, boundary: 0, kinds: {}, gen: false, arg_ok: null, ret_ok: null, faults: {} };
      stats.set(fn, st);
    }
    return st;
  }

  /** The innermost exported project function this runs in, else the innermost one at all. */
  function innermost() {
    const top = frames.getStore();
    for (let f = top; f; f = f.parent) if (f.fn.exported) return f.fn;
    return top?.fn ?? null;
  }

  const whereOf = (fn) => (fn ? `${fn.module}.${fn.qualname}` : null);

  function wrapper(file) {
    const table = spec.files[file]?.functions;
    if (!table) return undefined;
    const w = (i, fn) => (typeof fn === "function" ? wrap({ file, ...table[i] }, fn, w.table) : fn);
    w.table = table;
    w.method = (i, cls, name, isStatic) => {
      const on = isStatic ? cls : cls.prototype;
      const d = Object.getOwnPropertyDescriptor(on, name);
      if (typeof d?.value === "function") Object.defineProperty(on, name, { ...d, value: w(i, d.value) });
    };
    w.swap = (i, fn, module, set) => {
      const n = w(i, fn);
      if (n === fn) return;
      set(n);
      if (module.exports === fn) module.exports = n;
      else for (const k of Object.keys(module.exports ?? {})) if (module.exports[k] === fn) module.exports[k] = n;
    };
    return w;
  }

  function wrap(fn, f) {
    return new Proxy(f, {
      apply(target, self, args) {
        const parent = frames.getStore();
        return frames.run({ fn, parent }, () => watch(fn, target, self, args, parent));
      },
    });
  }

  let watch = (fn, target, self, args) => Reflect.apply(target, self, args);

  // ---------------------------------------------------------------- scan

  function scan() {
    handler = new boundary.Watcher();
    handler.before = (req) => {
      out.boundary_total++;
      const fn = innermost();
      if (!fn) return;
      const st = note(fn);
      st.boundary++;
      st.kinds[req.kind] = (st.kinds[req.kind] ?? 0) + 1;
    };
    watch = (fn, target, self, args) => {
      const st = note(fn);
      st.calls++;
      const look = st.calls <= 3;
      if (look) st.arg_ok = codec.can(args) && st.arg_ok !== false;
      const seen = (v) => {
        if (look) st.ret_ok = codec.can(v) && st.ret_ok !== false;
      };
      const r = Reflect.apply(target, self, args);
      if (isPromise(r)) {
        st.promise = true;
        return r.then((v) => (seen(v), v));
      }
      seen(r);
      return r;
    };
  }

  // ---------------------------------------------------------------- chaos, bench

  function live() {
    const plan = cfg.seed === null || cfg.seed === undefined ? { type: "none" }
      : { type: "seeded", seed: cfg.seed, rate: cfg.rate ?? 0.2, outage_rate: cfg.outage_rate ?? 0.05, salt: "" };
    if (cfg.seed !== null && cfg.seed !== undefined) globalThis[Symbol.for("ras-anything.seed")] = cfg.seed;
    const clock = new faults.LiveClock(cfg.scale ?? 0.05, cfg.hang_s ?? 2, (where) => {
      out.hangs++;
      out.uncaught = { type: "ChaosHang", msg: "boundary call with no timeout hung (chaos)", hang: true, where };
      finish();
      process.reallyExit(70);
    });
    faults.patchAbortSignal((ms, fire) => faults.real.setTimeout(fire, ms).unref());
    handler = new boundary.Faulting(plan, clock);
    handler.where = () => whereOf(innermost());
    handler.onAttempt = (req, rec) => {
      out.boundary_total++;
      const fn = innermost();
      scopes.getStore()?.forEach((list) => list.push(rec));
      if (!fn) return;
      rec.fn = whereOf(fn);
      const st = note(fn);
      st.boundary++;
      if (rec.fault) st.faults[rec.fault] = (st.faults[rec.fault] ?? 0) + 1;
    };
    if (cfg.mode === "bench") bench();
  }

  // ---------------------------------------------------------------- bench: what the enabled functions did

  const scopes = new AsyncLocalStorage(); // one list per wrapped call in progress; every attempt goes to all of them
  const swaps = (globalThis[Symbol.for("ras-anything.swaps")] ??= new Map());

  function bench() {
    const { isDeepStrictEqual } = require("node:util");
    const ids = new Map((cfg.targets ?? []).map((t) => [`${fs.realpathSync(t.file)}\n${t.qualname}`, t.id]));
    out.stats = {};
    watch = (fn, target, self, args) => {
      const id = ids.get(`${fn.file}\n${fn.qualname}`);
      if (!id) return Reflect.apply(target, self, args);
      const st = (out.stats[id] ??= { calls: 0, ok: 0, raised: {}, fallback: 0, lat: [], hardened: false });
      const attempts = [];
      const t0 = now();
      st.calls++;
      const raised = (e) => {
        st.lat.push((now() - t0) / 1000);
        const name = e?.name ?? typeof e;
        st.raised[name] = (st.raised[name] ?? 0) + 1;
      };
      const returned = (v) => {
        st.lat.push((now() - t0) / 1000);
        const mod = swaps.get(id)?.module;
        st.hardened = Boolean(mod);
        let fb = mod?.RAS_FALLBACK;
        const declared = fb !== undefined && fb !== null;
        try {
          if (typeof fb === "function") fb = fb(...args);
        } catch {}
        // a fallback only counts when this very call saw a request fail that never got a good answer
        const answered = new Set(attempts.filter((a) => !a.failed).map((a) => a.key));
        if (declared && isDeepStrictEqual(v, fb) && attempts.some((a) => !answered.has(a.key))) st.fallback++;
        else st.ok++;
      };
      return scopes.run([...(scopes.getStore() ?? []), attempts], () => {
        let r;
        try {
          r = Reflect.apply(target, self, args);
        } catch (e) {
          raised(e);
          throw e;
        }
        if (!isPromise(r)) {
          returned(r);
          return r;
        }
        return r.then((v) => (returned(v), v), (e) => {
          raised(e);
          throw e;
        });
      });
    };
  }

  // ---------------------------------------------------------------- record

  function record() {
    const target = cfg.target;
    const max = cfg.max_calls ?? 500;
    const current = new AsyncLocalStorage();
    const records = [];
    out.recorded = out.seen = out.skipped = 0;
    out.snapshot = null;
    handler = new boundary.Recorder();
    const keep = (rec, req, o, dur) => {
      if (!rec) return;
      try {
        rec.io.push({ req: codec.enc(boundary.reqDict(req)), out: codec.enc(boundary.recorded(o)), dur: round(dur) });
      } catch { // an answer that isn't data: this call can't be replayed
        rec.bad = true;
      }
    };
    handler.after = (req, o, dur) => keep(current.getStore(), req, o, dur);
    handler.onLive = (req) => {
      const rec = current.getStore();
      return (o, dur) => keep(rec, req, o, dur);
    };
    watch = (fn, f, self, args) => {
      if (fn.qualname !== target.qualname || current.getStore()) return Reflect.apply(f, self, args);
      out.seen++;
      out.snapshot ??= { env: codec.enc({ ...process.env }), globals: {} };
      let rec = null;
      if (records.length < max) {
        try {
          rec = { args: codec.enc(args), kwargs: {}, io: [] };
        } catch {
          out.skipped++;
        }
      }
      if (!rec) return Reflect.apply(f, self, args);
      const t0 = now();
      const done = (set) => {
        rec.dur = round((now() - t0) / 1000);
        const { bad } = rec;
        delete rec.bad;
        try {
          if (bad || records.length >= max) throw new Error();
          set();
          records.push(rec);
        } catch {
          out.skipped++;
        }
      };
      const returned = (v) => done(() => (rec.result = codec.enc(v)));
      const raised = (e) => done(() => (rec.exc = dumpError(e)));
      return current.run(rec, () => {
        let r;
        try {
          r = Reflect.apply(f, self, args);
        } catch (e) {
          raised(e);
          throw e;
        }
        if (!isPromise(r)) {
          returned(r);
          return r;
        }
        return r.then((v) => (returned(v), v), (e) => {
          raised(e);
          throw e;
        });
      });
    };
    onFinish = () => {
      out.recorded = records.length;
      const doc = { format: "ras-anything-cassette", version: 1, lang: "node", calls: records };
      fs.writeFileSync(cfg.calls_path, JSON.stringify(doc, null, 1) + "\n");
    };
  }

  // ---------------------------------------------------------------- start / finish

  let onFinish = () => {};
  ({ scan, chaos: live, bench: live, record })[cfg.mode]();
  boundary.install(handler);
  const agentKey = Symbol.for("ras-anything.agent");
  Object.defineProperty(globalThis, agentKey, { value: wrapper, configurable: true });
  hooks.install(spec.files);

  process.on("uncaughtExceptionMonitor", (err) => {
    out.uncaught = { type: err?.name ?? typeof err, msg: String(err?.message ?? err).slice(0, 300), hang: false, where: place(err) };
  });
  process.on("exit", finish);

  /** module.function of the innermost frame of the user's code. */
  function place(err) {
    for (const line of String(err?.stack ?? "").split("\n")) {
      const m = /^\s+at (?:async )?(?:(?:Object|Module)\.)?(?:new )?(?:([\w$.<>#]+)(?: \[as [^\]]+\])? \()?((?:file:\/\/)?\/[^)]*?):(\d+):\d+\)?$/.exec(line);
      if (!m) continue;
      let file = m[2];
      try {
        if (file.startsWith("file:")) file = fileURLToPath(file);
      } catch {
        continue;
      }
      const rel = path.relative(cfg.root, file);
      if (rel.startsWith("..") || /(^|\/)(node_modules|_ras_hardened|\.ras)\//.test(rel)) continue;
      const table = spec.files[file]?.functions ?? [];
      const line = Number(m[3]);
      const inside = table.find((f) => f.line <= line && line <= f.end);
      const mod = table[0]?.module ?? rel.replace(/\.\w+$/, "").replaceAll("/", ".");
      return inside ? `${mod}.${inside.qualname}` : m[1] ? `${mod}.${m[1].replace(/^.*\./, "")}` : `${mod}.<module>`;
    }
    for (let e = err; e !== null && typeof e === "object"; e = e.cause) if (boundary.blame.has(e)) return boundary.blame.get(e);
    return null;
  }

  function finish() {
    if (finished) return;
    finished = true;
    out.functions = [...stats].map(([fn, st]) => ({ file: fn.file, line: fn.line, name: fn.name, module: fn.module, qualname: fn.qualname, ...st }));
    if (handler.attempts) out.attempts = handler.attempts;
    for (const st of Object.values(out.stats ?? {})) {
      const lat = st.lat.sort((a, b) => a - b);
      delete st.lat;
      st.p50 = lat.length ? lat[Math.floor(lat.length / 2)] : null;
      st.p99 = lat.length ? lat[Math.min(lat.length - 1, Math.floor(0.99 * lat.length))] : null;
    }
    try {
      onFinish();
      fs.writeFileSync(cfg.out, JSON.stringify(out));
    } catch (e) {
      process.stderr.write(`ras-anything: couldn't write results: ${e}\n`);
    }
  }

  function round(x) {
    return Math.round(x * 1e6) / 1e6;
  }

  function isPromise(v) {
    return v !== null && typeof v === "object" && typeof v.then === "function" && v instanceof Promise;
  }

  /** The app (and its workers and forks) shouldn't see our --require. */
  function dropExecArg() {
    const i = process.execArgv.findIndex((a, k, all) => a === "--require" && all[k + 1] === __filename);
    if (i >= 0) process.execArgv.splice(i, 2);
  }
}
