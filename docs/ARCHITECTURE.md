# Architecture

ras-anything is one language-neutral pipeline plus, per language, an **adapter** (runs in the
ras-anything process) and an **agent** (runs inside the app's process). They exchange JSON only. This
file is the contract: a new language implements the adapter interface and the agent protocol below.

```
                    ras-anything process                        │        the app's process
                                                                │
 cli / mcp / go ─▶ scan ─▶ harden ─▶ verify ─▶ enable ─▶ bench  │
                     │        │         │                  │    │
                     ▼        ▼         ▼                  ▼    │
        runner.run(adapter, cmd, cfg) ── RAS_ANYTHING_CFG ──────┼─▶ agent: watches the boundary,
                     ▲                                          │   injects faults, records calls
                     └───────────── run output JSON ◀───────────┼──
        verify.replay(adapter, trials) ── job JSON ─────────────┼─▶ replay worker (agent language):
                     ▲                                          │   runs trials on a virtual clock
                     └───────────── raw results JSON ◀──────────┼──
        judge.py: classification, gates, availability           │
```

| file | role |
|---|---|
| `scan.py` `harden.py` `verify.py` `enable.py` `bench.py` `report.py` `go.py` `cli.py` `mcp.py` `doctor.py` `ui.py` | the pipeline. No language-specific branches: they call the adapter (`tests/test_lang.py` checks) |
| `faults.py` | fault kinds, the matrix, deterministic plans (and the clocks the Python agent uses) |
| `judge.py` | raw facts → ok / degraded / wrong / error / hang / miss, duplicates, gates, availability |
| `cassette.py` | reads, writes and displays `calls.json`; never turns it into objects |
| `signals.py` | static reliability signals and their weights |
| `paths.py` | project folder, portable paths, symlink-free writes |
| `lang/__init__.py` | adapter registry: `detect(cmd)`, `by_name(name)` |
| `lang/base.py` | the `LangAdapter` interface |
| `lang/python.py`, `lang/python_static.py` | the Python adapter; its AST signals |
| `agents/python/` | the Python agent: `_inject/sitecustomize.py` (loads it), `agent.py` (modes), `boundary.py` (interception), `values.py` (JSON ⇄ Python values), `worker.py` (replay) |
| `runtime.py` | the core of what hardened Python code may import (errors, log, clock, deadlines); vendored next to it by `enable` |
| `patterns/` | the resilience patterns as plugins: registry and contract (`__init__.py`), composition (`policy.py`), one module per pattern; vendored with the runtime. Reached through `runtime` (lazy names) |

**Dependency rules** (tests check them): core ↛ language specifics; `patterns/*` import `runtime` and the contract
only: no adapter, no pipeline module, no other pattern (`hedge` uses the drivers in `policy.py`). Patterns cooperate
through the `Call` a policy passes along, so any one can be off or missing. The runtime works with no pattern at all.
A new pattern = one module + its name in `patterns.NAMES` (and the same in `lang/node_agent/patterns/`).

## The adapter interface (`lang/base.py`)

Attributes: `name` (stored in scan/meta/cassette), `label`, `launchers`, `example`, `boundaries`,
`fence`, `hardened_name`, `private_env`, `bug_exceptions`.

| method | does |
|---|---|
| `detect` (cmd) | is this run command ours? From its first words only |
| `resolve` (cmd, cwd) | the argv that is really started (`uv run app.py` → `<env's python> app.py`) |
| `project_root` (cmd, cwd) | folder whose functions are the user's own |
| `attach` (cmd, env) | argv + env that load the agent. Python: `PYTHONPATH`. Node (phase B): `NODE_OPTIONS=--require …` |
| `interpreter` (project, meta) | executable for the replay worker |
| `describe` (fn) | static facts about a function the agent reported: `id, module, qualname, name, kind, async, line, in_main, signals, static_risk, refused`; `None` = not in the project's source. `signals` is `{signal: text}` with keys from `signals.SIGNALS`; `static_risk` is `signals.risk(signals)` |
| `hardenability` (d) | `None`, or why the function can't be swapped yet |
| `source` / `stub` / `callers` | the function's source; the hardened file to start from; call sites |
| `task_texts` (fn) | language-specific wording for TASK.md: `kind timeouts transient raise declare swallow errors equal helpers` |
| `record_target` (fn, project) | `cfg["target"]` for record mode |
| `record_meta` (fn, data, project) | extra `meta.json` fields the worker needs (Python: `sys_path`, `type_modules`) |
| `refuse_hardened` (path) | reason not to run this hardened file (it imports the harness), or `None` |
| `worker_cmd` (exe, job) | argv + env of the replay worker |
| `markers` / `block` / `insertion` | the marked block and where it goes in the module |
| `copy_name` / `runtime_file` / `runtime_modules` | names of the hardened copy, the vendored runtime and its pattern modules (`_ras_hardened/patterns/`) |
| `doctor` / `runtime_label` | optional: checks; "Python 3.13.5" for reports |

To add a language: `lang/<name>.py` + agent files, an entry in `lang.ADAPTERS`, package data in
`pyproject.toml`. For Node that means `lang/node.py` and `agents/node/` (a `--require` preload `.cjs`
and a worker, no npm dependencies).

## Wire format 1: run configuration (core → agent)

The agent reads JSON from the environment variable `RAS_ANYTHING_CFG`, **deletes the variable** (so
the app's own children aren't watched) and does nothing when it is missing. An agent must never break
the app: if it can't start, it says so on stderr and gets out of the way.

```jsonc
{
  "protocol": 1,
  "mode": "scan" | "chaos" | "record" | "bench",
  "root": "/abs/project/root",      // functions under here are the user's
  "project": "/abs/project",        // folder with .ras/ (paths inside strings become {"$proj"})
  "cwd": "/abs/cwd",
  "exclude": ["/abs/ras_anything"], // never the user's
  "out": "/tmp/ras-xyz.json",       // where to write the run output at exit
  // chaos and bench
  "seed": 3,                        // null in bench = no faults
  "rate": 0.25, "outage_rate": 0.05,
  "scale": 0.05, "hang_s": 2.0,     // live clock: injected waits × scale; a hang blocks hang_s, then is declared
  // record
  "target": {…},                    // whatever adapter.record_target returned
  "max_calls": 500, "calls_path": "/abs/.ras/hardens/<id>/calls.json",
  // bench
  "targets": [{"id", "module", "qualname", "in_main", "file"}]   // enabled functions to keep stats on
}
```

## Wire format 2: run output (agent → core)

Written to `cfg.out` as JSON when the process exits, however it exits (uncaught error included).

```jsonc
{
  "protocol": 1, "mode": "chaos",
  "runtime": {"name": "CPython", "version": "3.13.5"}, "executable": "/abs/python",
  "uncaught": null | {"type": "URLError", "msg": "…", "hang": false, "where": "weather.fetch_forecast"},
  "boundary_total": 12,             // boundary calls (attempts, in chaos/bench)
  "functions": [{                   // project functions seen; boundary work goes to the innermost one
    "file": "/abs/weather.py", "line": 9, "name": "fetch_forecast", "module": "weather",
    "calls": 12, "boundary": 12, "kinds": {"http": 12}, "gen": false,
    "arg_ok": true, "ret_ok": true, // could the first calls' arguments / results be stored as JSON?
    "faults": {"timeout": 1}        // chaos: faults injected while it was the innermost function
  }],
  "attempts": [{                    // chaos and bench: every dependency attempt, in order
    "key": "GET http://127.0.0.1/v1/forecast?city=Lima", "n": 0,   // n-th attempt of this key
    "method": "GET", "nonidempotent": false, "idem": null,          // Idempotency-Key header
    "fault": "timeout" | null,      // what was injected
    "reached": false,               // did the request reach the server?
    "failed": true                  // what was OBSERVED: see "Observed failure"
  }],
  "hangs": 0,
  // bench
  "stats": {"weather.fetch_forecast": {"calls", "ok", "fallback", "raised": {"URLError": 1}, "hardened": true, "p50", "p99"}},
  // record
  "recorded": 12, "seen": 12, "skipped": 0,
  "snapshot": {"env": {…}, "globals": {…}},   // before the first recorded call; values encoded as in cassettes
  "sys_path": […], "type_modules": […]        // Python's own, for record_meta
}
```

**Request key.** HTTP: `METHOD scheme://host/path?query` without the port, plus ` #<first 12 hex of the
body's sha256>` when there is a body. Subprocess: `run <argv…>`, the interpreter reduced to its name and
paths under the working directory made relative. The same logical request must have the same key in
every run, on every machine. Kind `socket` (key `CONNECT host[:port]`) marks a raw connection: it gets
connection faults only and is never recorded.

**Attribution.** Boundary work belongs to the innermost project function on the stack; helpers defined
inside a function count as that function.

**Observed failure** (`failed`): the dependency call ended in an exception (timeout, refused, reset…),
HTTP 5xx or 429, a process killed or exiting 75, or a body the agent cut. Other 4xx are answers. This is
the only evidence that can earn a fallback; text the app prints can't.

`stats[id].fallback` counts calls that returned the declared fallback **and** made, inside that same call,
a request that failed and never got a good answer.

## Faults

Kinds, effects and constants are in `faults.py` (`KINDS`, `MATRIX`, `DESCRIBE`, `CONSTANTS`):
`timeout` fires the caller's own timeout, or hangs when there is none (a POST/PUT/PATCH/DELETE reaches the
server either way) · `refused` · `http5xx` = 503 · `http429` = 429 + `Retry-After: 1` · `slow` = +3 s ·
`truncated` = half the body · `outage` = refused for the first 3 attempts. An injected failure must look
exactly like the API's own: the same exception class, or a response object with that status.

Which attempt gets which fault is decided by a *plan*, a pure function of (key, n):

| plan JSON | `fault(key, n)` |
|---|---|
| `{"type": "none"}` | none |
| `{"type": "first", "fault": F, "key": K \| null}` | attempt 0 gets F (`outage`: attempts 0–2 get `refused`); only key K unless null |
| `{"type": "seeded", "seed": S, "rate": R, "outage_rate": O, "salt": T}` | `refused` if n < 3 and u(S,T,key,"outage") < O; else if u(S,T,key,n,"p") < R: `KINDS[floor(u(S,T,key,n,"k") * 6)]` |

`u(parts…)` = the first 8 bytes of sha256 of the parts joined with `|`, as a big-endian integer, divided
by 2^64. Numbers are written as Python's `str` does (`3`, not `3.0`).

A hang is raised as something ordinary error handling can't swallow (Python: `ChaosHang`, a
`BaseException`) and reported even if it was swallowed.

## Cassette: `.ras/hardens/<id>/calls.json`

```jsonc
{
  "format": "ras-anything-cassette", "version": 1, "lang": "python",
  "calls": [{
    "args": [V…], "kwargs": {"name": V},       // a method's args start with self, a classmethod's with the class
    "result": V  |  "exc": E,                  // exactly one
    "dur": 0.0021,
    "io": [{"req": REQ, "out": OUT, "dur": 0.0019}]   // dependency exchanges, in order
  }]
}
```

Async functions are recorded from the call to the end of the await, each with its own `io` even when
several run concurrently (Python: a context variable).

- **REQ** `{"kind": "http" | "requests" | "httpx" | "aiohttp" | "subprocess" | …, "method", "url", "body_sha", "idempotency_key", "timeout", "argv", "key"}`.
  Request headers and bodies are not stored.
- **OUT** `{"status", "reason", "headers": [[k, v]…], "body": V, "url"}`, or
  `{"returncode", "stdout": V, "stderr": V}`, or `{"exc": E}`.
- **E** `{"type": "URLError", "module": "urllib.error", "msg": "…"}` plus, where the type needs it, `errno`,
  `reason` (an E or a string), `cmd`, `timeout`, `returncode`. A loader rebuilds only types from a fixed
  allowlist (Python: builtins, socket, ssl, urllib.error, http.client, subprocess, json.decoder, asyncio,
  requests, httpx and aiohttp exceptions; must be `Exception` subclasses). Anything else becomes a
  `RemoteError` carrying the recorded name. Loading never imports, evaluates or constructs what the file
  names.
- **V** plain JSON, or an object with exactly one key starting with `$`:

| tag | value |
|---|---|
| `{"$bytes": b64}` `{"$bytearray": b64}` | binary data |
| `{"$float": "nan" \| "inf" \| "-inf"}` | floats JSON lacks (JS agents also use `"-0"`) |
| `{"$str": b64}` | text that isn't valid UTF-8 |
| `{"$proj": [mark, text]}` | a string in which `mark` stands for the project's folder |
| `{"$dict": [[k, v]…]}` `{"$odict": …}` | a dict with non-string keys (or keys starting with `$`); an OrderedDict |
| `{"$tuple": […]}` `{"$set": […]}` `{"$frozenset": […]}` `{"$complex": [re, im]}` | |
| `{"$datetime"}` `{"$date"}` `{"$time"}` `{"$timedelta": [d, s, µs]}` `{"$decimal"}` `{"$uuid"}` `{"$path"}` | |
| `{"$object": [module, qualname, [[attr, V]…]]}` | instance of one of the project's own classes: rebuilt without running `__init__` |
| `{"$enum": [m, q, name]}` `{"$namedtuple": [m, q, […]]}` `{"$class": [m, q]}` | of the project's own classes |
| `{"$ext": [kind, state]}` | plumbing recreated fresh: locks, loggers, `requests.Session`, `httpx.Client` (default headers without credentials) |
| reserved for the Node agent | `$undefined`, `$bigint`, `$map`, `$typed`, `$regexp` |

An unknown tag is an error. Values that don't fit are refused at record time (the call is counted in
`skipped`), never approximated.

## Replay worker (verify)

`adapter.worker_cmd(exe, job)` starts one process per batch of trials. Stdout and stderr belong to the
app's code; the answer goes to `job.out`.

```jsonc
// job
{"protocol": 1, "lang": "python", "project": "/abs", "meta": {…meta.json…},
 "cassette": "/abs/…/calls.json", "hardened": "/abs/…/hardened.py",
 "constants": {"slow_s": 3.0, "hang_cap_s": 60.0, "outage_k": 3, "retry_after": "1"},
 "trials": [{"call": 0, "side": "original" | "hardened", "plan": {…}, "seed": 12345}],
 "out": "/tmp/…/out.json"}

// answer
{"protocol": 1, "fallback": null | "callable" | "<repr>", "results": [ one per trial, same order ]}
// or
{"protocol": 1, "fatal": {"stage": "import hardened.py", "message": "…traceback…"}}
```

The worker imports the original module (with `RAS_ANYTHING_DISABLE=1`, the recorded `env`, `globals`
and working directory), then the hardened file, then runs the trials in order in one process. For each:
fresh arguments decoded from the cassette, boundary calls served from that call's `io` by key (first
unused match, else the last match), faults from the plan, a virtual clock (sleeps and timeouts cost no
real time; `slow` and recorded durations are added to it; event-loop timers advance it too, so
`asyncio.sleep` and `wait_for` work), an event loop of its own for async functions, randomness seeded with `seed`. No network: a
request that isn't in the recording raises an uncatchable miss.

```jsonc
// one result: facts, no verdicts
{"outcome": "return" | "raise" | "hang" | "miss",
 "latency": 1.25,              // virtual seconds
 "matches": true,              // returned a value equal to the recorded one, or raised the recorded type
 "is_fallback": false,         // returned a value equal to the module's RAS_FALLBACK (a value, or called with the same arguments)
 "logs": 1,                    // stderr lines containing "trace_id"
 "attempts": [ …as in the run output… ],
 "miss": null | "request the original never made: …",
 "exc", "exc_type", "cause_type", "structured",
 "state": {"args": digest, "self": {attr: digest}},   // what the call left in its arguments (no-fault trials)
 "result": "<short repr>", "expected": "<short repr>"}
```

**Judging** (`judge.py`, in the ras-anything process): *hang* if it hung or took longer than the budget ·
*miss* · *ok* if `matches` · *degraded* if `is_fallback` in a fault trial, at least one attempt has
`failed`, some request of the original stayed unanswered (its key has no attempt without `failed`), and
the give-up was logged · *error* for other exceptions · otherwise *wrong*. A declared fallback that isn't
earned is *wrong*. On the happy path the hardened call must also leave its arguments as the original
does.

Trials: no fault on every recorded call; then for the first 60 calls the original reproduces, every
`MATRIX` fault on first attempts, three seeded mixes (p = 0.3), and, for calls with two or more distinct
requests, every fault on each request alone. Gates: see `judge.gates`.

## Files in `.ras/`

| file | committed? | |
|---|---|---|
| `scan.json` `state.json` `bench.json` `clean.stdout` | yes | no absolute paths (`paths.portable_cmd`, `paths.scrub`) |
| `hardens/<id>/{meta,calls,verify}.json`, hardened file, `TASK.md`, `agent.log` | yes | recordings hold real arguments and responses: review them |
| `runs/launcher.local` | no (gitignored) | this machine's interpreter and exact command |

`enable` writes exactly these places, after checking that none of them (nor any folder on the way) is a
symlink: the module (one marked block), `_ras_hardened/<copy>`, `_ras_hardened/<runtime>` and
`_ras_hardened/patterns/`. `disable`
validates every entry of `state.json` before it changes anything.
