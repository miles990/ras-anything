# Build 1: RAS-anything v0.1 (builder: Claude Opus 5.5)

Date: 2026-11-23. Host: Linux 6.12.113 x86_64, 8 CPUs, Python 3.13.5. Venv at `.venv` (`pip install -e .`).
I did not grade this build myself; this file records what was run and what it printed.

## What works (spec item → evidence)

| spec | command | evidence |
|---|---|---|
| S-1 scan | `ras-anything scan -- python app.py` | ranked tables in README; `examples/*/.ras/scan.json` |
| S-2 harden | `ras-anything harden <id> --agent claude --model opus` | `examples/*/.ras/hardens/<id>/{calls.pkl,TASK.md,meta.json,hardened.py,agent.log}` |
| S-3 verify | `ras-anything verify <id>` | `verify.json` per id; three negative controls rejected in `tests/test_pipeline.py` |
| S-4 enable/disable | `ras-anything enable|disable <id>` | test `test_good_passes_and_enable_disable_is_byte_for_byte` (sha256 equal after disable); example sources committed disabled |
| S-5 bench | `ras-anything bench [--degraded-marker X]` | `examples/*/.ras/bench.json` (20 seeds each) |
| S-6 report | `ras-anything report` | `examples/*/RAS-REPORT.md`, `examples/*/ras-card.svg` (rendered with headless Chrome and checked) |
| S-7 doctor | `ras-anything doctor` | checks python, claude/codex, git clean, .ras writable, requests |
| S-8 go | `ras-anything go --agent claude --model opus [--degraded-marker unavailable] -- python app.py` | `/tmp/go-weather.log`, `/tmp/go-payments.log` (exit 0 both); full excerpts below |
| S-9 agents | `ras-anything mcp`; `.claude-plugin/`; `commands/ras.md`; `skills/ras-anything/SKILL.md` | MCP initialize, tools/list (8 tools) and tools/call `ras_status` answered over stdio; unit test `McpTest` |
| S-10 runtime | `ras_anything/runtime.py` | `tests/test_runtime.py` (jitter bounds, no 4xx retry, Retry-After, budget, breaker states, RasError, log_event, call_with_timeout) |
| S-11 examples | `examples/weather`, `examples/payments` | hardened code written by `claude -p --model opus` through `--agent claude` (3 agent runs, logs kept) |
| S-12 README | `README.md` | numbers come only from the runs below |

Tests: `python -m unittest` → `Ran 28 tests in 1.6s, OK` (offline). The positive control runs verify on the agent-hardened examples; it was not skipped.

## Real output excerpts

weather `go` (verify step; the agent passed on its first try, 34 s):
```
  happy-path replay (no faults)            12/12 identical  ✓
  availability under faults         32.5%         ▲ 100.0%  ✓
  hangs                                15                0  ✓
  p99 latency (budget 5.00s)       60.00s            3.60s  ✓
```
payments `verify billing.charge`:
```
  availability under faults         76.9%         ▲ 100.0%  ✓
  silent wrong results                 17                0  ✓
  duplicate side effects               38                0  ✓
  ! the original double-sends: call #0 ({'id': 'INV-1001', ...} under timeout
    original: POST #0 timeout (reached server), POST #1 ok → 1 duplicate
    hardened: POST #0 timeout, POST #1 ok → 0 duplicates (one Idempotency-Key: charge-INV-1001)
```
payments bench (charge + render_receipt enabled, 20 seeds, no degraded marker):
```
  app availability            5.0%   ▲ 90.0%  exit 0, output correct or declared-degraded
    identical output          5.0%   ▲ 50.0%  strict: byte-identical to the clean run
  crashes                    60.0%   ▼ 10.0%
  hangs                      15.0%    ▼ 0.0%
  wrong output               20.0%    ▼ 0.0%
  duplicate side effects         5       ▼ 0
  p50 wall                   1.18s     1.27s
  p99 wall                   3.63s   ▲ 3.85s
  hardened failures: URLError in app.main ×2
```

## Measured numbers (committed artifacts)

| | original | hardened |
|---|---:|---:|
| weather verify availability (120 trials) | 32.5% | 100.0% |
| weather bench app availability (20 seeds, marker `unavailable`) | 40.0% | 100.0% |
| weather bench crashes / hangs | 45.0% / 15.0% | 0.0% / 0.0% |
| weather bench p50 / p99 run | 94ms / 2.10s | 338ms / 1.04s |
| payments verify `charge` availability / dups (160 trials) | 76.9% / 38 | 100.0% / 0 |
| payments verify `render_receipt` availability (160 trials) | 33.1% | 100.0% |
| payments bench app availability (20 seeds) | 5.0% | 90.0% |
| payments bench duplicate charges | 5 | 0 |

## Tool bugs the real runs found (fixed, then re-measured)

1. The fault key included the upstream's random port, so bench pairs didn't see the same faults. Keys now ignore the port. The weather `go` was re-run from scratch afterwards.
2. Replay ran under `sys.executable` of the CLI (`python3`) while the app recorded `python`, so subprocess argv never matched. The agent diagnosed this in `billing.render_receipt/agent.log`. Verify now replays under the recorded interpreter. I re-recorded and re-verified the agent's unchanged code.
3. `runtime.is_transient` didn't treat EAGAIN (`BlockingIOError`) as transient. Fixed and covered by a test.
4. Strict output equality counted declared fallbacks as "wrong". Bench now counts a run as degraded when a hardened function returned its declared `RAS_FALLBACK` in that run, and still reports byte-identical output separately.

After fixes 2–4 the committed verify/bench/report artifacts were re-measured with the final tool. The hardened code is the agents' code, unchanged (see each REPRODUCE.md).

## Known gaps

- `go` hardens only the top-ranked function. In payments that alone left app availability flat (5% → 5%, while dups went 5 → 0) until `render_receipt` was hardened too. `go` doesn't yet loop over every function above a score threshold.
- The remaining payments crashes are in `app.main` (the ledger read). Hardening an entry `main` that wraps everything would record a cassette of the whole run, so it isn't practical in v0.1.
- Bench wall times are noisy on this shared host. In one earlier bench run two runs took 10.7 s and 20.9 s; re-run alone, those seeds took 1.2 s and 1.9 s. Verify latencies are virtual and exact.
- Verify checks that a fallback is declared, not that it is used only for transient failures. The payments `charge` agent returns `None` (pending) on 4xx too, which matches the original's caller contract.
- Python only; top-level functions and staticmethods; urlopen / subprocess.run / requests boundaries only; no async.
- The `--agent claude` allowlist (Read/Edit/Write/Glob/Grep, `ras-anything verify|status`) blocked the render agent from running extra diagnostics, though it still identified the tool bug.
- Scan's score weights (0.4 static / 0.6 measured; signal weights in `static.py`) are judgment calls and haven't been validated on other apps.
- First-use comprehension (≤ 30 s) has not been tested with a blind reader.
