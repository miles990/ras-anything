// Replay worker: runs trials of recorded calls, original or hardened, on a virtual clock, and reports raw
// facts. It decides nothing: classification and gates are in ras_anything/judge.py, in another process.
//
// Usage: node worker.cjs <job.json>   (protocol: docs/ARCHITECTURE.md)
"use strict";
const fs = require("node:fs");
const module_ = require("node:module");
const path = require("node:path");
const util = require("node:util");
const { createHash } = require("node:crypto");
const { pathToFileURL } = require("node:url");
const { AsyncLocalStorage } = require("node:async_hooks");
const faults = require("./faults.cjs");
const boundary = require("./boundary.cjs");
const hooks = require("./hooks.cjs");
const runtime = require("./runtime.cjs");
const { Codec, dumpError, matches } = require("./values.cjs");

const { isDeepStrictEqual, inspect } = util;
const tick = () => new Promise((r) => faults.real.setImmediate(r));
const nap = (ms) => new Promise((r) => faults.real.setTimeout(r, ms));
const stderrWrite = process.stderr.write;

class Fatal extends Error {
  constructor(stage, e) {
    super(typeof e === "string" ? e : clean(e?.stack ?? String(e)));
    this.stage = stage;
  }
}

/** A stack without the harness's own frames. */
const clean = (stack) => stack.split("\n").filter((l) => !l.includes(__dirname) && !l.includes("node:internal")).join("\n");

// ---------------------------------------------------------------- replay

/** Serves dependency calls from one recorded call's cassette, with faults from a plan. */
class Replay extends boundary.Faulting {
  constructor(clock) {
    super({ type: "none" }, clock);
    this.trial = null;
    this.misses = [];
  }

  begin(trial, io, plan) {
    this.reset();
    this.setPlan(plan);
    this.trial = trial;
    this.io = io;
    this.used = new Set();
    this.misses = [];
  }

  setPlan(plan) {
    this.plan = faults.plan(plan);
  }

  miss(what) {
    const msg = this.trial === null ? `dependency call outside a replayed call: ${what}` : `request the original never made: ${what}`;
    this.misses.push(what);
    throw new boundary.ReplayMiss(msg);
  }

  source(req) {
    if (stale()) throw new boundary.ReplayMiss("left over from an earlier call");
    if (this.trial === null || current.getStore() !== this.trial) return this.miss(req.key);
    let at = -1;
    for (const [i, x] of this.io.entries()) {
      if (x.req.key !== req.key) continue;
      at = i;
      if (!this.used.has(i)) break;
    }
    if (at < 0) return this.miss(req.key);
    return () => {
      this.used.add(at);
      return { ...this.io[at].out, _dur: this.io[at].dur ?? 0 };
    };
  }

  async perform(req, _live, ctl) {
    if (stale()) return new Promise(() => {});
    return super.perform(req, this.source(req), ctl);
  }

  performSync(req, _live, timedOut) {
    return super.performSync(req, this.source(req), timedOut);
  }
}

const current = new AsyncLocalStorage(); // the trial this code belongs to
let replaying = null;
const stale = () => current.getStore() !== undefined && current.getStore() !== replaying?.trial;

// ---------------------------------------------------------------- driving async code on virtual time

/** Resources that will produce an event by themselves, counted. Timers are ours, pipes are our stdio. */
function pending() {
  const n = new Map();
  for (const k of process.getActiveResourcesInfo()) if (k !== "Timeout") n.set(k, (n.get(k) ?? 0) + 1);
  return n;
}

const busyBeyond = (base) => [...pending()].some(([k, n]) => n > (base.get(k) ?? 0));

/** Moves the clock until `done()`; false when nothing can happen any more. */
async function drive(clock, done, untilS) {
  const base = pending();
  for (let spins = 0; ;) {
    await tick();
    if (done()) return true;
    if (busyBeyond(base) && spins++ < 400) {
      if (spins > 20) await nap(1);
      continue;
    }
    spins = 0;
    await tick(); // (what the last event set off may take another turn)
    if (done()) return true;
    if (clock.elapsed() > untilS) return false;
    if (!clock.advance(clock.start + untilS * 1000)) {
      if (busyBeyond(base)) continue;
      await tick();
      return done();
    }
  }
}

// ---------------------------------------------------------------- job

async function runJob(job) {
  Object.assign(faults.C, job.constants);
  const { meta, project } = job;
  const codec = new Codec(project);
  const file = fs.realpathSync(path.join(project, meta.file));
  const hardenedFile = fs.realpathSync(job.hardened);
  Object.assign(process.env, codec.dec(meta.env ?? {}));
  process.env.RAS_ANYTHING_DISABLE = "1";
  try {
    process.chdir(path.join(project, meta.cwd ?? "."));
  } catch {}

  const clock = new faults.VirtualClock();
  const handler = (replaying = new Replay(clock));
  boundary.install(handler);
  boundary.seal((what) => handler.miss(what));
  clock.install();
  for (const name of ["boundary", "faults", "hooks", "values", "fakes", "worker"]) delete require.cache[path.join(__dirname, `${name}.cjs`)];

  // hardened code is resolved as if it were where enable puts it: _ras_hardened/ next to the module
  const home = path.join(path.dirname(file), "_ras_hardened", path.basename(hardenedFile));
  const runtimeFile = path.join(__dirname, "runtime.cjs");
  hooks.install(job.node_files, (specifier, parent, required) => {
    if (parent !== hardenedFile || /^node:/.test(specifier)) return null;
    if (specifier === "./_runtime.cjs") return { specifier: required ? runtimeFile : pathToFileURL(runtimeFile).href };
    if (!required) return { specifier, parent: home };
    if (/^\.\.?(\/|$)/.test(specifier)) return { specifier: path.resolve(path.dirname(home), specifier) };
    return module_.isBuiltin(specifier) ? null : { specifier: module_.createRequire(home).resolve(specifier) };
  });

  let original = null;
  const scopeKey = Symbol.for("ras-anything.scope");
  globalThis[scopeKey] = (id, names, fn) => {
    if (id !== meta.id) return;
    original = fn;
    runtime.enable({ id, names, original: () => {}, load: () => ({}) });
  };
  try {
    await import(pathToFileURL(file).href);
    if (handler.misses.length) throw new Error(`the module makes a dependency call when it is loaded: ${handler.misses[0]}`);
    original = original?.();
    if (typeof original !== "function") throw new Error(`${meta.qualname} isn't a function of ${meta.file} any more`);
  } catch (e) {
    throw new Fatal("import original", e);
  }
  delete globalThis[scopeKey];

  let calls;
  try {
    const doc = JSON.parse(fs.readFileSync(job.cassette, "utf8"));
    if (doc.format !== "ras-anything-cassette" || doc.version !== 1 || doc.lang !== "node") throw new Error("not a Node cassette of this version (run harden again)");
    calls = doc.calls.map((c) => {
      codec.dec([c.args, c.result ?? null]);
      return { ...c, io: codec.dec(c.io) };
    });
  } catch (e) {
    throw new Fatal(`read ${path.basename(job.cassette)}`, `${e.name}: ${e.message}`);
  }

  let hardened;
  let mod;
  try {
    // the way the enabled block loads it: synchronously
    mod = module_.createRequire(hardenedFile)(hardenedFile);
    hardened = mod[meta.name];
    if (typeof hardened !== "function") throw new Error(`it exports no function ${meta.name}`);
  } catch (e) {
    throw new Fatal(`import ${path.basename(job.hardened)}`, e);
  }
  const fallback = mod.RAS_FALLBACK ?? null;

  let crash = null; // an error nothing handled: it would end a real process
  let wake = () => {};
  const onCrash = (e) => {
    crash ??= { error: e };
    wake();
  };
  process.on("uncaughtException", onCrash);
  process.on("unhandledRejection", onCrash);

  let seq = 0;
  async function trial(t) {
    const call = calls[t.call];
    const fn = t.side === "hardened" ? hardened : original;
    const args = codec.dec(call.args);
    const me = `trial ${++seq}`;
    clock.reset();
    if (t.gap_s) clock.t += t.gap_s * 1000; // one of a sequence: time passes in between, and what the code learned stays
    else runtime.resetState();
    clock.start = clock.t;
    handler.begin(me, call.io, t.plan);
    let s = t.seed >>> 0;
    Math.random = () => {
      s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
      return s / 2 ** 32;
    };
    runtime.seed(t.seed);
    crash = null;
    let logged = "";
    process.stderr.write = (chunk, enc, cb) => {
      logged += typeof chunk === "string" ? chunk : Buffer.from(chunk).toString();
      (typeof enc === "function" ? enc : cb)?.();
      return true;
    };
    const end = { outcome: null, value: undefined, error: null, latency: 0 };
    const settle = (outcome, v) => {
      if (end.outcome) return;
      end.outcome = outcome;
      end.latency = clock.elapsed();
      if (outcome === "raise") end.error = v;
      else end.value = v;
    };
    const over = () => end.outcome !== null || crash !== null;
    try {
      await current.run(me, async () => {
        try {
          const r = runtime.withTrace(() => fn(...args));
          if (r !== null && typeof r === "object" && typeof r.then === "function") r.then((v) => settle("return", v), (e) => settle("raise", e));
          else settle("return", r);
        } catch (e) {
          settle("raise", e);
        }
      });
      const finished = await drive(clock, over, clock.limitS);
      if (!finished && !end.outcome) {
        end.latency = Math.max(clock.elapsed(), faults.C.hang_cap_s);
        end.outcome = "hang";
      }
      if (end.outcome !== "hang") { // what it still does in the background counts too, its time doesn't
        const until = clock.elapsed() + 30;
        await drive(clock, () => crash !== null || clock.timers.size === 0, until);
        await tick();
      }
    } finally {
      process.stderr.write = stderrWrite;
    }
    if (crash && end.outcome !== "hang") {
      end.outcome = "raise";
      end.error = crash.error;
      end.latency ||= clock.elapsed();
    }
    let miss = null;
    if (clock.hangs) end.outcome = "hang";
    else if (handler.misses.length) {
      end.outcome = "miss";
      miss = `request the original never made: ${handler.misses[0]}`;
    }
    handler.trial = null;
    const { outcome, value, error } = end;
    const expected = call.exc ? undefined : codec.dec(call.result);
    const same = outcome === "raise" ? Boolean(call.exc) && matches(error, call.exc)
      : outcome === "return" && !call.exc && isDeepStrictEqual(value, expected);
    const cause = error instanceof Error ? error.cause ?? null : null;
    return {
      outcome, latency: Math.round(end.latency * 1e6) / 1e6, matches: same,
      is_fallback: outcome === "return" && fallback !== null && isDeepStrictEqual(value, await fallbackValue(fallback, codec.dec(call.args))),
      miss, logs: logged.split("\n").filter((l) => l.includes('"trace_id"')).length,
      result: outcome === "return" ? short(value) : null,
      expected: call.exc ? `throws ${call.exc.type}` : short(expected),
      exc: error === null ? null : `${typeName(error)}: ${String(error?.message ?? error).slice(0, 200)}`,
      exc_type: error === null ? null : bugType(error), cause_type: cause === null ? null : bugType(cause),
      structured: error !== null && (error instanceof runtime.RasError || cause !== null),
      state: t.plan.type === "none" ? { args: digest(args) } : null,
      attempts: handler.attempts.map(({ key, n, fault, reached, failed, idem, method, nonidempotent }) =>
        ({ key, n, fault, reached, failed, idem, method, nonidempotent })),
    };
  }

  const plain = new Codec(null);
  function digest(v) {
    try {
      return createHash("sha256").update(JSON.stringify(plain.enc(v))).digest("hex").slice(0, 16);
    } catch {
      return "?";
    }
  }

  const results = [];
  for (const t of job.trials) results.push(await trial(t));
  return { results, fallback: fallback === null ? null : typeof fallback === "function" ? "callable" : short(fallback) };
}

async function fallbackValue(fb, args) {
  if (typeof fb !== "function") return fb;
  try {
    return await fb(...args);
  } catch {
    return Symbol("no fallback");
  }
}

const typeName = (e) => (e instanceof Error || e instanceof DOMException ? e.name : typeof e);

/** The error's class, where a failed dependency isn't mistaken for a bug: fetch reports both as TypeError. */
function bugType(e) {
  const n = typeName(e);
  return n === "TypeError" && (e.cause || e.message === "terminated" || e.message === "fetch failed") ? "FetchError" : n;
}

function short(v, n = 300) {
  const s = inspect(v, { depth: 4, breakLength: Infinity });
  return s.length <= n ? s : s.slice(0, n - 1) + "…";
}

async function main() {
  const job = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const write = fs.writeFileSync;
  const exit = process.exit.bind(process);
  let res;
  try {
    res = await runJob(job);
  } catch (e) {
    process.stderr.write = stderrWrite;
    res = { fatal: e instanceof Fatal ? { stage: e.stage, message: e.message } : { stage: "replay", message: e?.stack ?? String(e) } };
  }
  write(job.out, JSON.stringify({ protocol: 1, ...res }));
  exit(0);
}

main();
