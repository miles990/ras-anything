# orders-node: app availability under faults 0.0% → 90.0%

`node app.mjs`, 20 whole-app runs per variant (seeds 1–20, original and hardened interleaved, same fault seed per pair), faults injected live at the dependency boundary with p=0.1 per attempt.

![ras-anything card](../../docs/cards/orders-node.svg)

| whole app | original | hardened |
|---|---:|---:|
| availability (exit 0, output correct or declared-degraded) | 0.0% | **90.0%** |
| byte-identical output (strict) | 0.0% | 90.0% |
| crashes | 75.0% | 10.0% |
| hangs | 15.0% | 0.0% |
| wrong output | 10.0% | 0.0% |
| duplicate non-idempotent side effects | 6 | 0 |
| p50 / p99 run time | 316ms / 4.13s | 2.74s / 4.62s |
| faults injected (total) | 52 | 240 |

A run counts as *degraded* when its output differs only where a hardened function returned its declared `RAS_FALLBACK` in that run (and symmetrically for the original if its output matches a declared-degraded output) or on lines containing `PENDING`.

- original crashes/hangs: Error in orders.shippingQuote ×7, TypeError in orders.getItem ×4, SyntaxError in ? ×2, Error in orders.printLabel ×2, hang in orders.printLabel ×1, hang in orders.shippingQuote ×1, hang in orders.getItem ×1
- hardened crashes/hangs: TypeError in app.<module> ×1, TimeoutError in app.<module> ×1

## Verified functions

Each hardened function was replayed on its recorded real calls (dependency answers from the recording, no network, virtual clock), then under the fault matrix below, next to the original.

| function | happy path | availability under faults | wrong | hangs | p99 (budget) | dup side effects | verdict |
|---|---|---|---|---|---|---|---|
| `orders.charge` | 12/12 identical | 78.3% → **100.0%** | 12 → 0 | 14 → 0 | 10.30s → 3.60s (5.00s) | 28 → 0 | ✓ pass |
| `orders.getItem` | 12/12 identical | 29.2% → **100.0%** | 30 → 0 | 16 → 0 | 60.00s → 4.00s (5.00s) | 0 → 0 | ✓ pass |
| `orders.printLabel` | 12/12 identical | 32.5% → **100.0%** | 0 → 0 | 13 → 0 | 60.00s → 3.52s (5.00s) | 0 → 0 | ✓ pass |
| `orders.shippingQuote` | 12/12 identical | 39.2% → **100.0%** | 23 → 0 | 14 → 0 | 60.00s → 3.52s (5.00s) | 0 → 0 | ✓ pass |

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

Host: Linux 6.12.113+ x86_64, 8 CPUs, Node.js 24.22.1 · 2026-11-23T13:42:30+0800

Undo: `ras-anything disable <id>` removes the swap byte for byte; `RAS_ANYTHING_DISABLE=1` runs the originals.

<sub>Made with ras-anything · design inspired by [fast-anything](https://github.com/EL4CTEO/fast-anything) (MIT)</sub>
