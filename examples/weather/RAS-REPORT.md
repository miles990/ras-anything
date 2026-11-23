# weather: app availability under faults 40.0% → 100.0%

`python app.py`, 20 whole-app runs per variant (seeds 1–20, original and hardened interleaved, same fault seed per pair), faults injected live at the dependency boundary with p=0.1 per attempt.

![ras-anything card](../../docs/cards/weather.svg)

| whole app | original | hardened |
|---|---:|---:|
| availability (exit 0, output correct or declared-degraded) | 40.0% | **100.0%** |
| byte-identical output (strict) | 40.0% | 100.0% |
| crashes | 45.0% | 0.0% |
| hangs | 15.0% | 0.0% |
| wrong output | 0.0% | 0.0% |
| duplicate non-idempotent side effects | 0 | 0 |
| retry amplification (dependency attempts per call of the enabled functions; no faults: 1.00) | 1.00 | 1.19 |
| … for calls that met an outage (6 / 11 calls) | 1.00 | 4.00 |
| p50 / p99 run time | 101ms / 2.11s | 330ms / 1.25s |
| faults injected (total) | 14 | 49 |

A run counts as *degraded* when its output differs only where a hardened function returned its declared `RAS_FALLBACK` in that run (and symmetrically for the original if its output matches a declared-degraded output) or on lines containing `unavailable`.

- original crashes/hangs: URLError in weather.fetch_forecast ×6, hang in weather.fetch_forecast ×3, JSONDecodeError in weather.fetch_forecast ×2, HTTPError in weather.fetch_forecast ×1

## Verified functions

Each hardened function was replayed on its recorded real calls (dependency answers from the recording, no network, virtual clock), then under the fault matrix below, next to the original.

| function | happy path | availability under faults | wrong | hangs | p99 (budget) | dup side effects | verdict |
|---|---|---|---|---|---|---|---|
| `weather.fetch_forecast` | 12/12 identical | 32.5% → **100.0%** | 0 → 0 | 15 → 0 | 60.00s → 3.60s (5.00s) | 0 → 0 | ✓ pass |

### Retry amplification and outages

Attempts per call over the fault matrix, and in a sustained outage: 40 recorded calls in a row, 0.5 s apart, with the dependency down (after 10 healthy ones, before 90 with it back, 1 s apart). Nothing is reset in between, so breakers, retry budgets and caches work as they would. `answered` = ok + degraded; `back after` = virtual seconds from the first call after the outage until a real answer.

| function | healthy | fault matrix | in the outage | answered in the outage | back after |
|---|---|---|---|---|---|
| `weather.fetch_forecast` | 1.00 → 1.00 | 1.00 → 1.88 | 1.00 → **4.00** | 0 → 40 of 40 | 0 s → 0 s |

## SLI / SLO & error-budget burn

Configured targets (`.ras/slo.json`, source: `bench`): availability ≥ **99.0%**, p99 ≤ **5.00s**, error rate ≤ **1.0%**.

| SLI / SLO metric | original | hardened | target |
|---|---:|---:|---:|
| availability | 40.0% | 100.0% | ≥ 99.0% |
| p99 latency | 2.11s | 1.25s | ≤ 5.00s |
| error rate | 60.0% | 0.0% | ≤ 1.0% |
| error-budget burn | 6000% | 0% | ≤ 100% |
| SLO verdict | ✗ missed | **✓ met** | met |

## Serviceability (OTel JSON logs & diagnostics)

Measured by running the log-only diagnoser over captured faulted-run `stderr` (OTel JSON log records with W3C `traceparent`). See [docs/RUNBOOK.md](docs/RUNBOOK.md) for log queries, health snapshot, incident replay (`ras-anything replay`), and rollback.

| serviceability metric | original | hardened |
|---|---:|---:|
| fault detection rate (from logs) | 0.0% | 100.0% |
| root-cause diagnosis accuracy (dep + fault) | 0.0% | 71.3% |
| median lines to diagnosis | unavailable | 1 |
| time to restore (bad-enable rollback fixture) | unavailable | unavailable |

## Agent experience (RFC 9457 structured errors)

Measured with the deterministic policy agent against plain exception text vs RFC 9457 `RasError.problem` objects, plus PII/secret leak scanning.

| AX benchmark (10-cause catalog) | plain error | RFC 9457 `Problem` |
|---|---:|---:|
| next-action accuracy | 30.0% | **100.0%** |
| mean tokens to decide | 42.20 | 105.80 |
| mean decision steps | 2.90 | 1.00 |
| PII / secret leaks scanned | — | **0** |

## Fault model

| fault | effect at the boundary |
|---|---|
| `timeout` | no answer: caller's timeout fires; with no timeout it hangs (POST/PUT still reach the server) |
| `refused` | connection refused |
| `http5xx` | HTTP 503 (subprocess: exit 75 EX_TEMPFAIL) |
| `http429` | HTTP 429 with Retry-After: 1 |
| `slow` | +3 s latency, then the correct answer |
| `truncated` | body (or stdout) cut in half |
| `outage` | dependency down (refused) for 3 consecutive attempts, then recovers |
| `mixed` | seeded mix: each attempt faults with p=0.3, kind drawn uniformly |

Whole-app bench: each dependency attempt draws its fault from a hash of (seed, request, attempt number), so original and hardened see the same fault on the same attempt. Outages hit 5% of requests. Injected waits are scaled ×0.05 in real time and a call with no timeout is declared hung after 2s; the real local upstream answers everything else.

Host: Linux 6.12.113+ x86_64, 8 CPUs, Python 3.13.5 · 2026-11-23T16:48:34+0800

Undo: `ras-anything disable <id>` removes the swap byte for byte; `RAS_ANYTHING_DISABLE=1` runs the originals.

<sub>Made with ras-anything · design inspired by [fast-anything](https://github.com/EL4CTEO/fast-anything) (MIT)</sub>
