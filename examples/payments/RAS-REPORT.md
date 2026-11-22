# payments: app availability under faults 0.0% → 90.0%

`/workspace/ras-anything/.venv/bin/python app.py`, 20 whole-app runs per variant (seeds 1–20, original and hardened interleaved, same fault seed per pair), faults injected live at the dependency boundary with p=0.1 per attempt.

![ras-anything card](../../docs/cards/payments.svg)

| whole app | original | hardened |
|---|---:|---:|
| availability (exit 0, output correct or declared-degraded) | 0.0% | **90.0%** |
| byte-identical output (strict) | 0.0% | 45.0% |
| crashes | 60.0% | 10.0% |
| hangs | 25.0% | 0.0% |
| wrong output | 15.0% | 0.0% |
| duplicate non-idempotent side effects | 5 | 0 |
| p50 / p99 run time | 1.79s / 3.94s | 1.28s / 3.87s |
| faults injected (total) | 44 | 144 |

A run counts as *degraded* when its output differs only where a hardened function returned its declared `RAS_FALLBACK` in that run (and symmetrically for the original if its output matches a declared-degraded output).

- original crashes/hangs: JSONDecodeError in billing.render_receipt ×9, hang in billing.render_receipt ×5, BlockingIOError in billing.render_receipt ×3
- hardened crashes/hangs: URLError in app.main ×2

## Verified functions

Each hardened function was replayed on its recorded real calls (dependency answers from the recording, no network, virtual clock), then under the fault matrix below, next to the original.

| function | happy path | availability under faults | wrong | hangs | p99 (budget) | dup side effects | verdict |
|---|---|---|---|---|---|---|---|
| `billing.charge` | 16/16 identical | 76.9% → **100.0%** | 17 → 0 | 20 → 0 | 13.50s → 4.50s (5.00s) | 38 → 0 | ✓ pass |
| `billing.render_receipt` | 16/16 identical | 33.1% → **100.0%** | 0 → 0 | 18 → 0 | 60.00s → 1.17s (5.00s) | 0 → 0 | ✓ pass |

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

Host: Linux 6.12.113+ x86_64, 8 CPUs, Python 3.13.5 · 2026-11-23T05:03:10+0800

Undo: `ras-anything disable <id>` removes the swap byte for byte; `RAS_ANYTHING_DISABLE=1` runs the originals.

<sub>Made with ras-anything · design inspired by [fast-anything](https://github.com/EL4CTEO/fast-anything) (MIT)</sub>
