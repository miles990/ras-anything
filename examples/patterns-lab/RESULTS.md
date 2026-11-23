# Pattern lab results

Made by `ras-anything bench --patterns --seeds 20` on Linux x86_64, Python 3.13.5, 2026-11-23. Seeds 1001 to 1020, virtual clock, so the numbers are reproducible. `off → on`, means over the seeds; **bold** = clearly better, ▼ = clearly worse (rule: docs/PATTERNS.md).

## Each pattern in the scenario it is meant for

| pattern | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | verdict |
|---|---|---|---|---|---|---|---|---|
| **Fallback hierarchy (stale cache, degraded value)**<br>`fallback` | outage, in the stack | python | 65.9% → **100.0%** | 0.07 s → 0.07 s | 0.0 → 0.0 | 0.67 → 0.67 | 0.04 → 0.04 | **adopted** |
|  |  | node | 65.9% → **100.0%** | 0.07 s → 0.07 s | 0.0 → 0.0 | 0.67 → 0.67 | 0.04 → 0.04 |  |
| **Total deadline, propagated**<br>`deadline` | layered calls, in the stack | python | 64.4% → 64.4% | 1.59 s → 1.59 s | 0.0 → 0.0 | 0.73 → 0.73 | 0.23 → 0.23 | **adopted** |
|  |  | node | 65.2% → 64.4% ▼ | 1.59 s → 1.59 s | 0.0 → 0.0 | 0.74 → 0.73 | 0.25 → **0.23** |  |
| **Bulkhead (concurrency cap)**<br>`bulkhead` | noisy neighbour, in the stack | python | 85.4% → 85.3% | 2.00 s → **0.07 s** | 0.0 → 0.0 | 0.87 → 0.86 | 0.74 → 0.73 | **opt-in** |
|  |  | node | 85.3% → 85.3% | 2.00 s → **0.07 s** | 0.0 → 0.0 | 0.87 → 0.86 | 0.74 → 0.73 |  |
| **Idempotency-Key per logical operation**<br>`idempotency` | lost answers, in the stack | python | 99.6% → 99.6% | 3.59 s → 3.59 s | 76.5 → **0.0** | 1.20 → 1.20 | — | **adopted** |
|  |  | node | 99.5% → 99.5% | 3.59 s → 3.59 s | 76.2 → **0.0** | 1.20 → 1.20 | — |  |
| **Retry budget (token bucket)**<br>`retry_budget` | outage, bare client | python | 66.6% → 66.5% | 0.27 s → **0.09 s** | 0.0 → 0.0 | 1.67 → **1.06** | 2.99 → **1.18** | **adopted** |
|  |  | node | 66.6% → 66.5% | 0.27 s → **0.09 s** | 0.0 → 0.0 | 1.67 → **1.06** | 2.99 → **1.18** |  |
| **Honour Retry-After (429/503)**<br>`retry_after` | rate limit, in the stack | python | 86.0% → **96.1%** | 0.08 s → 2.07 s ▼ | 0.0 → 0.0 | 1.04 → 1.33 ▼ | — | **adopted** |
|  |  | node | 86.0% → **96.1%** | 0.08 s → 2.07 s ▼ | 0.0 → 0.0 | 1.04 → 1.33 ▼ | — |  |
| **Retry with backoff and full jitter**<br>`retry` | transient, in the stack | python | 84.7% → **99.5%** | 2.00 s → 2.22 s ▼ | 0.0 → 0.0 | 1.00 → 1.17 ▼ | — | **adopted** |
|  |  | node | 84.7% → **99.5%** | 2.00 s → 2.21 s ▼ | 0.0 → 0.0 | 1.00 → 1.17 ▼ | — |  |
| **Circuit breaker**<br>`circuit_breaker` | retry storm, in the stack | python | 41.0% → **88.1%** | 4.09 s → **2.00 s** | 0.0 → 0.0 | 1.07 → **0.92** | 1.11 → **0.44** | **adopted** |
|  |  | node | 40.9% → **88.1%** | 4.09 s → **2.00 s** | 0.0 → 0.0 | 1.08 → **0.92** | 1.11 → **0.44** |  |
| **Adaptive throttling / load shedding**<br>`adaptive_throttle` | retry storm, bare client | python | 26.0% → **48.9%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 2.48 → **1.13** | 2.77 → 2.71 | **opt-in** |
|  |  | node | 26.0% → **49.3%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 2.48 → **1.13** | 2.78 → 2.72 |  |
| **Adaptive concurrency limit (AIMD)**<br>`adaptive_concurrency` | retry storm, in the stack | python | 88.1% → **92.3%** | 2.00 s → **1.36 s** | 0.0 → 0.0 | 0.92 → 0.92 | 0.44 → 0.43 | **opt-in** |
|  |  | node | 88.1% → **92.3%** | 2.00 s → **1.36 s** | 0.0 → 0.0 | 0.92 → 0.92 | 0.44 → 0.43 |  |
| **Hedged requests (reads)**<br>`hedge` | slow tail, in the stack | python | 100.0% → 100.0% | 1.56 s → **0.14 s** | 0.0 → 0.0 | 1.00 → 1.09 ▼ | — | **opt-in** |
|  |  | node | 100.0% → 100.0% | 1.56 s → **0.14 s** | 0.0 → 0.0 | 1.00 → 1.10 ▼ | — |  |
| **Attempt timeout**<br>`attempt_timeout` | blackhole, in the stack | python | 60.6% → **65.9%** | 4.80 s → **2.01 s** | 0.0 → 0.0 | 0.66 → 0.69 | 0.17 → **0.08** | **adopted** |
|  |  | node | 60.6% → **65.9%** | 4.80 s → **2.01 s** | 0.0 → 0.0 | 0.66 → 0.69 | 0.17 → **0.08** |  |
| **Full jitter (vs none)**<br>`jitter` | outage, in the stack | python | 66.0% → 65.9% | 0.10 s → **0.07 s** | 0.0 → 0.0 | 0.67 → 0.67 | 0.04 → 0.04 | **adopted** |
|  |  | node | 66.0% → 65.9% | 0.10 s → **0.07 s** | 0.0 → 0.0 | 0.67 → 0.67 | 0.04 → 0.04 |  |

## Verdicts

| pattern | verdict | clearly better (best three) | costs, in the stack (worst three) | not adopted because |
|---|---|---|---|---|
| Fallback hierarchy (stale cache, degraded value) | **adopted** |  `thundering herd` availability 17.5% → 100.0%<br>(bare) `thundering herd` availability 23.9% → 100.0%<br>(bare) `retry storm` availability 26.0% → 95.7% |  `layered calls` outage_amplification 0.23 → 0.56<br> `layered calls` amplification 0.73 → 0.85 | — |
| Total deadline, propagated | **adopted** | (bare) `noisy neighbour` availability 56.9% → 87.1%<br>(bare) `noisy neighbour` wasted 50.8% → 0.0%<br>(bare) `layered calls` wasted 26.5% → 0.0% | none | — |
| Bulkhead (concurrency cap) | **opt-in** | (bare) `retry storm` availability 26.0% → 79.6%<br>(bare) `noisy neighbour` availability 56.9% → 85.9%<br>(bare) `noisy neighbour` wasted 50.8% → 0.0% |  `retry storm` recovery_s 2.2 s → 59.8 s<br> `thundering herd` availability 17.5% → 2.5%<br> `layered calls` outage_amplification 0.23 → 0.49 | costs 15.0 points of availability in `thundering herd`; slows recovery by more than 2 s in `retry storm`; costs availability on balance (-34.2 points over the scenarios); costs availability on balance (-33.8 points over the scenarios) |
| Idempotency-Key per logical operation | **adopted** |  `lost answers` dups 76.5 → 0.0<br>(bare) `lost answers` dups 76.5 → 0.0 | none | — |
| Retry budget (token bucket) | **adopted** | (bare) `noisy neighbour` availability 56.9% → 77.4%<br>(bare) `retry storm` availability 26.0% → 41.0%<br>(bare) `noisy neighbour` wasted 50.8% → 15.9% | none | — |
| Honour Retry-After (429/503) | **adopted** |  `rate limit` availability 86.0% → 96.1%<br>(bare) `rate limit` availability 86.7% → 96.1% |  `rate limit` p99_s 0.08 s → 2.07 s<br> `rate limit` amplification 1.04 → 1.33 | — |
| Retry with backoff and full jitter | **adopted** |  `lost answers` availability 83.0% → 99.6%<br>(bare) `lost answers` availability 83.0% → 99.6%<br>(bare) `transient` availability 84.7% → 99.7% |  `rate limit` p99_s 0.07 s → 2.07 s<br> `layered calls` outage_amplification 0.11 → 0.23<br> `lost answers` p99_s 2.00 s → 3.59 s | — |
| Circuit breaker | **adopted** | (bare) `retry storm` availability 26.0% → 88.2%<br> `retry storm` availability 41.0% → 88.1%<br>(bare) `retry storm` recovery_s 75.0 s → 2.1 s |  `layered calls` availability 67.8% → 64.4%<br> `noisy neighbour` availability 86.6% → 85.4%<br> `thundering herd` availability 18.4% → 17.5% | — |
| Adaptive throttling / load shedding | **opt-in** | (bare) `retry storm` availability 26.0% → 48.9%<br>(bare) `thundering herd` amplification 2.64 → 1.01<br>(bare) `retry storm` amplification 2.48 → 1.13 | none | adds nothing clear to the stack |
| Adaptive concurrency limit (AIMD) | **opt-in** | (bare) `retry storm` availability 26.0% → 92.3%<br>(bare) `retry storm` recovery_s 75.0 s → 1.9 s<br>(bare) `noisy neighbour` availability 56.9% → 85.1% |  `transient` availability 99.5% → 64.3%<br> `lost answers` availability 99.6% → 74.6%<br> `thundering herd` availability 17.5% → 5.0% | costs 35.2 points of availability in `transient`; costs availability on balance (-77.9 points over the scenarios); costs availability on balance (-79.7 points over the scenarios) |
| Hedged requests (reads) | **opt-in** |  `slow tail` p99_s 1.56 s → 0.14 s<br>(bare) `slow tail` p99_s 1.56 s → 0.14 s<br>(bare) `lost answers` p99_s 3.59 s → 2.06 s |  `blackhole` outage_amplification 0.08 → 0.12<br> `layered calls` outage_amplification 0.23 → 0.29<br> `retry storm` availability 88.1% → 86.3% | costs availability on balance (-3.4 points over the scenarios); raises p99 or load without buying availability; changes the healthy scenario; slows recovery by more than 2 s in `retry storm`; costs availability on balance (-4.0 points over the scenarios) |
| Attempt timeout | **adopted** | (bare) `noisy neighbour` availability 27.2% → 56.9%<br> `lost answers` availability 88.2% → 99.6%<br>(bare) `lost answers` availability 88.2% → 99.6% |  `lost answers` amplification 1.06 → 1.20<br> `transient` amplification 1.08 → 1.17 | — |
| Full jitter (vs none) | **adopted** |  `outage` p99_s 0.10 s → 0.07 s<br>(bare) `rate limit` p99_s 0.32 s → 0.26 s<br>(bare) `outage` p99_s 0.31 s → 0.27 s | none | — |

## The bare client → the stack

| scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |
|---|---|---|---|---|---|---|---|---|
| healthy | python | 100.0% → 100.0% | 0.07 s → 0.07 s | 0.0 → 0.0 | 1.00 → 1.00 | — | 0.0% → 0.0% | — |
| healthy | node | 100.0% → 100.0% | 0.07 s → 0.07 s | 0.0 → 0.0 | 1.00 → 1.00 | — | 0.0% → 0.0% | — |
| transient | python | 99.7% → 99.5% | 2.23 s → 2.22 s | 0.0 → 0.0 | 1.17 → 1.17 | — | 0.0% → 0.0% | — |
| transient | node | 99.7% → 99.5% | 2.22 s → 2.21 s | 0.0 → 0.0 | 1.17 → 1.17 | — | 0.0% → 0.0% | — |
| slow tail | python | 100.0% → 100.0% | 1.56 s → 1.56 s | 0.0 → 0.0 | 1.00 → 1.00 | — | 0.0% → 0.0% | — |
| slow tail | node | 100.0% → 100.0% | 1.56 s → 1.56 s | 0.0 → 0.0 | 1.00 → 1.00 | — | 0.0% → 0.0% | — |
| outage | python | 66.6% → 65.9% | 0.27 s → 0.07 s | 0.0 → 0.0 | 1.67 → 0.67 | 2.99 → 0.04 | 0.0% → 0.0% | 0.0 s → 0.9 s |
| outage | node | 66.6% → 65.9% | 0.27 s → 0.07 s | 0.0 → 0.0 | 1.67 → 0.67 | 2.99 → 0.04 | 0.0% → 0.0% | 0.0 s → 0.8 s |
| blackhole | python | 71.1% → 65.9% | 5.00 s → 2.01 s | 0.0 → 0.0 | 1.65 → 0.69 | 2.79 → 0.08 | 0.0% → 0.0% | 0.0 s → 1.0 s |
| blackhole | node | 71.1% → 65.9% | 5.00 s → 2.01 s | 0.0 → 0.0 | 1.65 → 0.69 | 2.80 → 0.08 | 0.0% → 0.0% | 0.0 s → 1.0 s |
| retry storm | python | 26.0% → 88.1% | 5.00 s → 2.00 s | 0.0 → 0.0 | 2.48 → 0.92 | 2.77 → 0.44 | 0.0% → 0.0% | 75.0 s → 2.2 s |
| retry storm | node | 26.0% → 88.1% | 5.00 s → 2.00 s | 0.0 → 0.0 | 2.48 → 0.92 | 2.78 → 0.44 | 0.0% → 0.0% | 75.0 s → 2.1 s |
| thundering herd | python | 23.9% → 17.5% | 0.97 s → 0.71 s | 0.0 → 0.0 | 2.64 → 1.00 | — | 0.0% → 0.0% | — |
| thundering herd | node | 23.8% → 17.5% | 0.96 s → 0.71 s | 0.0 → 0.0 | 2.64 → 1.00 | — | 0.0% → 0.0% | — |
| noisy neighbour | python | 56.9% → 85.4% | 5.00 s → 2.00 s | 0.0 → 0.0 | 1.11 → 0.87 | 0.62 → 0.74 | 50.8% → 0.0% | 0.0 s → 0.9 s |
| noisy neighbour | node | 57.0% → 85.3% | 5.00 s → 2.00 s | 0.0 → 0.0 | 1.11 → 0.87 | 0.62 → 0.74 | 50.8% → 0.0% | 0.0 s → 0.9 s |
| rate limit | python | 86.7% → 96.1% | 0.26 s → 2.07 s | 0.0 → 0.0 | 1.28 → 1.33 | — | 0.0% → 0.0% | — |
| rate limit | node | 86.7% → 96.1% | 0.25 s → 2.07 s | 0.0 → 0.0 | 1.28 → 1.33 | — | 0.0% → 0.0% | — |
| lost answers | python | 99.6% → 99.6% | 3.59 s → 3.59 s | 76.5 → 0.0 | 1.20 → 1.20 | — | 0.0% → 0.0% | — |
| lost answers | node | 99.6% → 99.5% | 3.59 s → 3.59 s | 76.5 → 0.0 | 1.20 → 1.20 | — | 0.0% → 0.0% | — |
| layered calls | python | 73.5% → 64.4% | 5.00 s → 1.59 s | 0.0 → 0.0 | 2.03 → 0.73 | 3.93 → 0.23 | 26.5% → 0.0% | 0.0 s → 1.7 s |
| layered calls | node | 73.5% → 64.4% | 5.00 s → 1.59 s | 0.0 → 0.0 | 2.44 → 0.73 | 5.04 → 0.23 | 76.8% → 0.0% | 0.0 s → 1.7 s |

## The example apps

| function | variant | happy path | availability | p99 | dups | wrong | hangs | attempts per call | … in an outage | answered in the outage | back after | verify |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `weather.fetch_forecast` | stack | 12/12 | 100.0% | 3.51 s | 0 | 0 | 0 | 1.88 | 0.60 | 100% | 0.0 s | ✓ |
|  | - Fallback hierarchy (stale cache, degraded value) | 12/12 | 100.0% | 3.51 s | 0 | 0 | 0 | 1.88 | 0.60 | **0%** | 0.0 s | ✓ |
|  | - Total deadline, propagated | 12/12 | 100.0% | 3.51 s | 0 | 0 | 0 | 1.88 | 0.60 | 100% | 0.0 s | ✓ |
|  | + Bulkhead (concurrency cap) | 12/12 | 100.0% | 3.51 s | 0 | 0 | 0 | 1.88 | 0.60 | 100% | 0.0 s | ✓ |
|  | - Idempotency-Key per logical operation | 12/12 | 100.0% | 3.51 s | 0 | 0 | 0 | 1.88 | 0.60 | 100% | 0.0 s | ✓ |
|  | - Retry budget (token bucket) | 12/12 | 100.0% | 3.51 s | 0 | 0 | 0 | 1.88 | 0.60 | 100% | 0.0 s | ✓ |
|  | - Honour Retry-After (429/503) | 12/12 | 100.0% | 3.51 s | 0 | 0 | 0 | 1.88 | 0.60 | 100% | 0.0 s | ✓ |
|  | - Retry with backoff and full jitter | 12/12 | 100.0% | **3.40 s** | 0 | 0 | 0 | **1.00** | **0.55** | 100% | 0.0 s | ✓ |
|  | - Circuit breaker | 12/12 | 100.0% | 3.51 s | 0 | 0 | 0 | 1.88 | **2.58** | 100% | 0.0 s | ✓ |
|  | + Adaptive throttling / load shedding | 12/12 | 100.0% | **3.50 s** | 0 | **1** | 0 | **1.38** | **0.55** | 100% | 0.0 s | ✗ no_wrong |
|  | + Adaptive concurrency limit (AIMD) | 12/12 | 100.0% | 3.51 s | 0 | 0 | 0 | 1.88 | 0.60 | 100% | 0.0 s | ✓ |
|  | + Hedged requests (reads) | 12/12 | 100.0% | **1.01 s** | 0 | 0 | 0 | **1.99** | 0.60 | 100% | 0.0 s | ✓ |
|  | - Attempt timeout | 12/12 | 100.0% | **4.70 s** | 0 | 0 | 0 | **1.75** | 0.60 | 100% | 0.0 s | ✓ |
|  | - Full jitter (vs none) | 12/12 | 100.0% | **3.51 s** | 0 | 0 | 0 | 1.88 | 0.60 | 100% | 0.0 s | ✓ |
| `billing.charge` | stack | 16/16 | 100.0% | 4.70 s | 0 | 0 | 0 | 1.91 | 0.60 | 100% | 0.0 s | ✓ |
|  | - Fallback hierarchy (stale cache, degraded value) | 16/16 | **98.1%** | 4.70 s | 0 | 0 | 0 | 1.91 | 0.60 | **0%** | 0.0 s | ✓ |
|  | - Total deadline, propagated | 16/16 | **98.8%** | **6.44 s** | 0 | 0 | **2** | **1.92** | 0.60 | 100% | 0.0 s | ✗ no_hang, p99 |
|  | + Bulkhead (concurrency cap) | 16/16 | 100.0% | 4.70 s | 0 | 0 | 0 | 1.91 | 0.60 | 100% | 0.0 s | ✓ |
|  | - Idempotency-Key per logical operation | 16/16 | 100.0% | **3.40 s** | 0 | 0 | 0 | **1.00** | **0.55** | 100% | 0.0 s | ✓ |
|  | - Retry budget (token bucket) | 16/16 | 100.0% | 4.70 s | 0 | 0 | 0 | 1.91 | 0.60 | 100% | 0.0 s | ✓ |
|  | - Honour Retry-After (429/503) | 16/16 | 100.0% | 4.70 s | 0 | 0 | 0 | 1.91 | 0.60 | 100% | 0.0 s | ✓ |
|  | - Retry with backoff and full jitter | 16/16 | 100.0% | **3.40 s** | 0 | 0 | 0 | **1.00** | **0.55** | 100% | 0.0 s | ✓ |
|  | - Circuit breaker | 16/16 | 100.0% | 4.70 s | 0 | 0 | 0 | 1.91 | **2.58** | 100% | 0.0 s | ✓ |
|  | + Adaptive throttling / load shedding | 16/16 | 100.0% | **3.48 s** | 0 | **7** | 0 | **1.38** | **0.55** | 100% | 0.0 s | ✗ no_wrong |
|  | + Adaptive concurrency limit (AIMD) | 16/16 | 100.0% | 4.70 s | 0 | 0 | 0 | 1.91 | 0.60 | 100% | 0.0 s | ✓ |
|  | + Hedged requests (reads) | 16/16 | 100.0% | 4.70 s | 0 | 0 | 0 | 1.91 | 0.60 | 100% | 0.0 s | ✓ |
|  | - Attempt timeout | 16/16 | 100.0% | 4.70 s | 0 | 0 | 0 | **1.79** | 0.60 | 100% | 0.0 s | ✓ |
|  | - Full jitter (vs none) | 16/16 | 100.0% | 4.70 s | 0 | 0 | 0 | 1.91 | 0.60 | 100% | 0.0 s | ✓ |
| `orders.getItem` | stack | 12/12 | 100.0% | 4.00 s | 0 | 0 | 0 | 1.93 | 0.60 | 100% | 1.0 s | ✓ |
|  | - Fallback hierarchy (stale cache, degraded value) | 12/12 | 100.0% | 4.00 s | 0 | 0 | 0 | 1.93 | 0.60 | **0%** | 1.0 s | ✓ |
|  | - Total deadline, propagated | 12/12 | 100.0% | 4.00 s | 0 | 0 | 0 | 1.93 | 0.60 | 100% | 1.0 s | ✓ |
|  | + Bulkhead (concurrency cap) | 12/12 | 100.0% | 4.00 s | 0 | 0 | 0 | 1.93 | 0.60 | 100% | 1.0 s | ✓ |
|  | - Idempotency-Key per logical operation | 12/12 | 100.0% | 4.00 s | 0 | 0 | 0 | 1.93 | 0.60 | 100% | 1.0 s | ✓ |
|  | - Retry budget (token bucket) | 12/12 | 100.0% | 4.00 s | 0 | 0 | 0 | 1.93 | 0.60 | 100% | 1.0 s | ✓ |
|  | - Honour Retry-After (429/503) | 12/12 | 100.0% | **3.49 s** | 0 | 0 | 0 | 1.93 | 0.60 | 100% | 1.0 s | ✓ |
|  | - Retry with backoff and full jitter | 12/12 | **29.2%** | **3.40 s** | 0 | 0 | 0 | **1.00** | **0.55** | 100% | **0.0 s** | ✗ availability |
|  | - Circuit breaker | 12/12 | 100.0% | 4.00 s | 0 | 0 | 0 | 1.93 | **2.50** | 100% | **0.0 s** | ✓ |
|  | + Adaptive throttling / load shedding | 12/12 | **60.0%** | **3.49 s** | 0 | 0 | 0 | **1.39** | **0.55** | 100% | **5.0 s** | ✓ |
|  | + Adaptive concurrency limit (AIMD) | 12/12 | 100.0% | 4.00 s | 0 | 0 | 0 | 1.93 | 0.60 | 100% | 1.0 s | ✓ |
|  | + Hedged requests (reads) | 12/12 | 100.0% | **3.00 s** | 0 | 0 | 0 | **2.06** | 0.60 | 100% | 1.0 s | ✓ |
|  | - Attempt timeout | 12/12 | **86.7%** | **4.70 s** | 0 | 0 | 0 | **1.78** | 0.60 | 100% | 1.0 s | ✓ |
|  | - Full jitter (vs none) | 12/12 | 100.0% | 4.00 s | 0 | 0 | 0 | 1.93 | 0.60 | 100% | 1.0 s | ✓ |
| `orders.charge` | stack | 12/12 | 100.0% | 3.50 s | 0 | 0 | 0 | 1.95 | 0.60 | 100% | 1.0 s | ✓ |
|  | - Fallback hierarchy (stale cache, degraded value) | 12/12 | 100.0% | 3.50 s | 0 | 0 | 0 | 1.95 | 0.60 | **0%** | 1.0 s | ✓ |
|  | - Total deadline, propagated | 12/12 | 100.0% | 3.50 s | 0 | 0 | 0 | 1.95 | 0.60 | 100% | 1.0 s | ✓ |
|  | + Bulkhead (concurrency cap) | 12/12 | 100.0% | 3.50 s | 0 | 0 | 0 | 1.95 | 0.60 | 100% | 1.0 s | ✓ |
|  | - Idempotency-Key per logical operation | 12/12 | 100.0% | **3.40 s** | 0 | 0 | 0 | **1.00** | **0.55** | 100% | **0.0 s** | ✓ |
|  | - Retry budget (token bucket) | 12/12 | 100.0% | 3.50 s | 0 | 0 | 0 | 1.95 | 0.60 | 100% | 1.0 s | ✓ |
|  | - Honour Retry-After (429/503) | 12/12 | 100.0% | 3.50 s | 0 | 0 | 0 | 1.95 | 0.60 | 100% | 1.0 s | ✓ |
|  | - Retry with backoff and full jitter | 12/12 | 100.0% | **3.40 s** | 0 | 0 | 0 | **1.00** | **0.55** | 100% | **0.0 s** | ✓ |
|  | - Circuit breaker | 12/12 | 100.0% | 3.50 s | 0 | 0 | 0 | 1.95 | **2.50** | 100% | **0.0 s** | ✓ |
|  | + Adaptive throttling / load shedding | 12/12 | 100.0% | **3.49 s** | 0 | **9** | 0 | **1.41** | **0.55** | 100% | **5.0 s** | ✗ no_wrong |
|  | + Adaptive concurrency limit (AIMD) | 12/12 | 100.0% | 3.50 s | 0 | 0 | 0 | 1.95 | 0.60 | 100% | 1.0 s | ✓ |
|  | + Hedged requests (reads) | 12/12 | 100.0% | 3.50 s | 0 | 0 | 0 | 1.95 | 0.60 | 100% | 1.0 s | ✓ |
|  | - Attempt timeout | 12/12 | 100.0% | **4.70 s** | 0 | 0 | 0 | **1.82** | 0.60 | 100% | 1.0 s | ✓ |
|  | - Full jitter (vs none) | 12/12 | 100.0% | **3.50 s** | 0 | 0 | 0 | 1.95 | 0.60 | 100% | 1.0 s | ✓ |

## Every clear difference

### Fallback hierarchy (stale cache, degraded value) (`fallback`): adopted

| base | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |
|---|---|---|---|---|---|---|---|---|---|
| stack | outage | python | 65.9% → **100.0%** | 0.07 s → 0.07 s | 0.0 → 0.0 | 0.67 → 0.67 | 0.04 → 0.04 | 0.0% → 0.0% | 0.9 s → 0.9 s |
| stack | outage | node | 65.9% → **100.0%** | 0.07 s → 0.07 s | 0.0 → 0.0 | 0.67 → 0.67 | 0.04 → 0.04 | 0.0% → 0.0% | 0.8 s → 0.8 s |
| stack | blackhole | python | 65.9% → **100.0%** | 2.01 s → 2.01 s | 0.0 → 0.0 | 0.69 → 0.69 | 0.08 → 0.08 | 0.0% → 0.0% | 1.0 s → 1.0 s |
| stack | blackhole | node | 65.9% → **100.0%** | 2.01 s → 2.01 s | 0.0 → 0.0 | 0.69 → 0.69 | 0.08 → 0.08 | 0.0% → 0.0% | 1.0 s → 1.0 s |
| stack | retry storm | python | 88.1% → **100.0%** | 2.00 s → 2.00 s | 0.0 → 0.0 | 0.92 → 0.92 | 0.44 → 0.44 | 0.0% → 0.0% | 2.2 s → 2.2 s |
| stack | retry storm | node | 88.1% → **100.0%** | 2.00 s → 2.00 s | 0.0 → 0.0 | 0.92 → 0.92 | 0.44 → 0.44 | 0.0% → 0.0% | 2.1 s → 2.1 s |
| stack | thundering herd | python | 17.5% → **100.0%** | 0.71 s → 0.71 s | 0.0 → 0.0 | 1.00 → 1.00 | — | 0.0% → 0.0% | — |
| stack | thundering herd | node | 17.5% → **100.0%** | 0.71 s → 0.71 s | 0.0 → 0.0 | 1.00 → 1.00 | — | 0.0% → 0.0% | — |
| stack | noisy neighbour | python | 85.4% → **100.0%** | 2.00 s → 2.00 s | 0.0 → 0.0 | 0.87 → 0.87 | 0.74 → 0.74 | 0.0% → 0.0% | 0.9 s → 0.9 s |
| stack | noisy neighbour | node | 85.3% → **100.0%** | 2.00 s → 2.00 s | 0.0 → 0.0 | 0.87 → 0.87 | 0.74 → 0.74 | 0.0% → 0.0% | 0.9 s → 1.0 s |
| stack | rate limit | python | 96.1% → **100.0%** | 2.07 s → 2.07 s | 0.0 → 0.0 | 1.33 → 1.33 | — | 0.0% → 0.0% | — |
| stack | rate limit | node | 96.1% → **100.0%** | 2.07 s → 2.07 s | 0.0 → 0.0 | 1.33 → 1.33 | — | 0.0% → 0.0% | — |
| stack | lost answers | node | 99.5% → **100.0%** | 3.59 s → 3.60 s | 0.0 → 0.0 | 1.20 → 1.20 | — | 0.0% → 0.0% | — |
| stack | layered calls | python | 64.4% → **100.0%** | 1.59 s → 1.57 s | 0.0 → 0.0 | 0.73 → 0.85 ▼ | 0.23 → 0.56 ▼ | 0.0% → 0.0% | 1.7 s → **0.0 s** |
| stack | layered calls | node | 64.4% → **100.0%** | 1.59 s → 1.57 s | 0.0 → 0.0 | 0.73 → 0.85 ▼ | 0.23 → 0.56 ▼ | 0.0% → 0.0% | 1.7 s → **0.0 s** |
| bare | outage | python | 66.6% → **100.0%** | 0.27 s → 0.27 s | 0.0 → 0.0 | 1.67 → 1.67 | 2.99 → 2.99 | 0.0% → 0.0% | 0.0 s → 0.0 s |
| bare | outage | node | 66.6% → **100.0%** | 0.27 s → 0.27 s | 0.0 → 0.0 | 1.67 → 1.67 | 2.99 → 2.99 | 0.0% → 0.0% | 0.0 s → 0.0 s |
| bare | retry storm | python | 26.0% → **95.7%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 2.48 → 2.48 | 2.77 → 2.78 | 0.0% → 0.0% | 75.0 s → 75.0 s |
| bare | retry storm | node | 26.0% → **95.8%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 2.48 → 2.48 | 2.78 → 2.78 | 0.0% → 0.0% | 75.0 s → 75.0 s |
| bare | thundering herd | python | 23.9% → **100.0%** | 0.97 s → 0.97 s | 0.0 → 0.0 | 2.64 → 2.64 | — | 0.0% → 0.0% | — |
| bare | thundering herd | node | 23.8% → **100.0%** | 0.96 s → 0.97 s | 0.0 → 0.0 | 2.64 → 2.64 | — | 0.0% → 0.0% | — |
| bare | rate limit | python | 86.7% → **100.0%** | 0.26 s → 0.25 s | 0.0 → 0.0 | 1.28 → 1.28 | — | 0.0% → 0.0% | — |
| bare | rate limit | node | 86.7% → **100.0%** | 0.25 s → 0.25 s | 0.0 → 0.0 | 1.28 → 1.28 | — | 0.0% → 0.0% | — |

### Total deadline, propagated (`deadline`): adopted

| base | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |
|---|---|---|---|---|---|---|---|---|---|
| stack | layered calls | node | 65.2% → 64.4% ▼ | 1.59 s → 1.59 s | 0.0 → 0.0 | 0.74 → 0.73 | 0.25 → **0.23** | 0.0% → 0.0% | 1.1 s → 1.7 s |
| bare | noisy neighbour | python | 56.9% → **87.1%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.11 → 1.10 | 0.62 → 1.13 ▼ | 50.8% → **0.0%** | 0.0 s → 0.0 s |
| bare | noisy neighbour | node | 57.0% → **87.1%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.11 → 1.10 | 0.62 → 1.13 ▼ | 50.8% → **0.0%** | 0.0 s → 0.0 s |
| bare | layered calls | python | 73.5% → 73.5% | 5.00 s → 4.80 s | 0.0 → 0.0 | 2.03 → **1.90** | 3.93 → **3.57** | 26.5% → **0.0%** | 0.0 s → 0.0 s |
| bare | layered calls | node | 73.5% → 73.5% | 5.00 s → 4.80 s | 0.0 → 0.0 | 2.44 → **1.90** | 5.04 → **3.57** | 76.8% → **0.0%** | 0.0 s → 0.0 s |

### Bulkhead (concurrency cap) (`bulkhead`): opt-in

| base | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |
|---|---|---|---|---|---|---|---|---|---|
| stack | transient | python | 99.5% → 98.1% ▼ | 2.22 s → 2.22 s | 0.0 → 0.0 | 1.17 → 1.16 | — | 0.0% → 0.0% | — |
| stack | transient | node | 99.5% → 98.2% ▼ | 2.21 s → 2.22 s | 0.0 → 0.0 | 1.17 → 1.16 | — | 0.0% → 0.0% | — |
| stack | blackhole | python | 65.9% → 65.9% | 2.01 s → **0.07 s** | 0.0 → 0.0 | 0.69 → 0.67 | 0.08 → **0.02** | 0.0% → 0.0% | 1.0 s → 1.0 s |
| stack | blackhole | node | 65.9% → 65.9% | 2.01 s → **0.07 s** | 0.0 → 0.0 | 0.69 → 0.67 | 0.08 → **0.02** | 0.0% → 0.0% | 1.0 s → 1.0 s |
| stack | retry storm | python | 88.1% → 79.6% ▼ | 2.00 s → **0.38 s** | 0.0 → 0.0 | 0.92 → **0.80** | 0.44 → **0.38** | 0.0% → 0.0% | 2.2 s → 59.8 s ▼ |
| stack | retry storm | node | 88.1% → 79.9% ▼ | 2.00 s → **0.38 s** | 0.0 → 0.0 | 0.92 → **0.80** | 0.44 → **0.38** | 0.0% → 0.0% | 2.1 s → 59.6 s ▼ |
| stack | thundering herd | python | 17.5% → 2.5% ▼ | 0.71 s → **0.11 s** | 0.0 → 0.0 | 1.00 → **0.03** | — | 0.0% → 0.0% | — |
| stack | thundering herd | node | 17.5% → 2.5% ▼ | 0.71 s → **0.11 s** | 0.0 → 0.0 | 1.00 → **0.03** | — | 0.0% → 0.0% | — |
| stack | noisy neighbour | python | 85.4% → 85.3% | 2.00 s → **0.07 s** | 0.0 → 0.0 | 0.87 → 0.86 | 0.74 → 0.73 | 0.0% → 0.0% | 0.9 s → 1.0 s |
| stack | noisy neighbour | node | 85.3% → 85.3% | 2.00 s → **0.07 s** | 0.0 → 0.0 | 0.87 → 0.86 | 0.74 → 0.73 | 0.0% → 0.0% | 0.9 s → 1.1 s |
| stack | rate limit | python | 96.1% → 85.8% ▼ | 2.07 s → **0.12 s** | 0.0 → 0.0 | 1.33 → **0.86** | — | 0.0% → 0.0% | — |
| stack | rate limit | node | 96.1% → 85.8% ▼ | 2.07 s → **0.12 s** | 0.0 → 0.0 | 1.33 → **0.86** | — | 0.0% → 0.0% | — |
| stack | layered calls | python | 64.4% → **65.5%** | 1.59 s → 1.55 s | 0.0 → 0.0 | 0.73 → 0.83 ▼ | 0.23 → 0.49 ▼ | 0.0% → 0.0% | 1.7 s → 1.1 s |
| stack | layered calls | node | 64.4% → **65.5%** | 1.59 s → 1.56 s | 0.0 → 0.0 | 0.73 → 0.83 ▼ | 0.23 → 0.50 ▼ | 0.0% → 0.0% | 1.7 s → 1.1 s |
| bare | transient | python | 99.7% → 98.3% ▼ | 2.23 s → 2.22 s | 0.0 → 0.0 | 1.17 → 1.16 | — | 0.0% → 0.0% | — |
| bare | transient | node | 99.7% → 98.4% ▼ | 2.22 s → 2.22 s | 0.0 → 0.0 | 1.17 → 1.16 | — | 0.0% → 0.0% | — |
| bare | blackhole | python | 71.1% → 65.4% ▼ | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.65 → **0.71** | 2.79 → **0.17** | 0.0% → 0.0% | 0.0 s → 1.1 s ▼ |
| bare | blackhole | node | 71.1% → 65.4% ▼ | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.65 → **0.71** | 2.80 → **0.17** | 0.0% → 0.0% | 0.0 s → 1.1 s ▼ |
| bare | retry storm | python | 26.0% → **79.6%** | 5.00 s → **0.38 s** | 0.0 → 0.0 | 2.48 → **0.80** | 2.77 → **0.38** | 0.0% → 0.0% | 75.0 s → 59.8 s |
| bare | retry storm | node | 26.0% → **79.9%** | 5.00 s → **0.38 s** | 0.0 → 0.0 | 2.48 → **0.80** | 2.78 → **0.38** | 0.0% → 0.0% | 75.0 s → 59.6 s |
| bare | thundering herd | python | 23.9% → 2.5% ▼ | 0.97 s → **0.11 s** | 0.0 → 0.0 | 2.64 → **0.03** | — | 0.0% → 0.0% | — |
| bare | thundering herd | node | 23.8% → 2.5% ▼ | 0.96 s → **0.11 s** | 0.0 → 0.0 | 2.64 → **0.03** | — | 0.0% → 0.0% | — |
| bare | noisy neighbour | python | 56.9% → **85.9%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.11 → **0.93** | 0.62 → 0.85 ▼ | 50.8% → **0.0%** | 0.0 s → 0.5 s |
| bare | noisy neighbour | node | 57.0% → **85.9%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.11 → **0.93** | 0.62 → 0.85 ▼ | 50.8% → **0.0%** | 0.0 s → 0.3 s |
| bare | rate limit | python | 86.7% → 85.7% ▼ | 0.26 s → **0.08 s** | 0.0 → 0.0 | 1.28 → **0.86** | — | 0.0% → 0.0% | — |
| bare | rate limit | node | 86.7% → 85.7% ▼ | 0.25 s → **0.07 s** | 0.0 → 0.0 | 1.28 → **0.87** | — | 0.0% → 0.0% | — |
| bare | layered calls | python | 73.5% → 66.9% ▼ | 5.00 s → 5.00 s | 0.0 → 0.0 | 2.03 → **0.79** | 3.93 → **0.36** | 26.5% → **2.5%** | 0.0 s → 1.0 s ▼ |
| bare | layered calls | node | 73.5% → 66.7% ▼ | 5.00 s → **2.09 s** | 0.0 → 0.0 | 2.44 → **0.85** | 5.04 → **0.56** | 76.8% → **0.0%** | 0.0 s → 1.0 s ▼ |

### Idempotency-Key per logical operation (`idempotency`): adopted

| base | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |
|---|---|---|---|---|---|---|---|---|---|
| stack | lost answers | python | 99.6% → 99.6% | 3.59 s → 3.59 s | 76.5 → **0.0** | 1.20 → 1.20 | — | 0.0% → 0.0% | — |
| stack | lost answers | node | 99.5% → 99.5% | 3.59 s → 3.59 s | 76.2 → **0.0** | 1.20 → 1.20 | — | 0.0% → 0.0% | — |
| bare | lost answers | python | 99.6% → 99.6% | 3.59 s → 3.59 s | 76.5 → **0.0** | 1.20 → 1.20 | — | 0.0% → 0.0% | — |
| bare | lost answers | node | 99.6% → 99.6% | 3.59 s → 3.59 s | 76.5 → **0.0** | 1.20 → 1.20 | — | 0.0% → 0.0% | — |

### Retry budget (token bucket) (`retry_budget`): adopted

| base | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |
|---|---|---|---|---|---|---|---|---|---|
| stack | layered calls | python | 65.0% → 64.4% | 1.60 s → 1.59 s | 0.0 → 0.0 | 0.81 → **0.73** | 0.47 → **0.23** | 0.0% → 0.0% | 1.3 s → 1.7 s |
| stack | layered calls | node | 65.1% → 64.4% | 1.60 s → 1.59 s | 0.0 → 0.0 | 0.81 → **0.73** | 0.47 → **0.23** | 0.0% → 0.0% | 1.3 s → 1.7 s |
| bare | outage | python | 66.6% → 66.5% | 0.27 s → **0.09 s** | 0.0 → 0.0 | 1.67 → **1.06** | 2.99 → **1.18** | 0.0% → 0.0% | 0.0 s → 0.0 s |
| bare | outage | node | 66.6% → 66.5% | 0.27 s → **0.09 s** | 0.0 → 0.0 | 1.67 → **1.06** | 2.99 → **1.18** | 0.0% → 0.0% | 0.0 s → 0.0 s |
| bare | blackhole | python | 71.1% → 66.9% ▼ | 5.00 s → **4.09 s** | 0.0 → 0.0 | 1.65 → **1.06** | 2.79 → **1.17** | 0.0% → 0.0% | 0.0 s → 0.0 s |
| bare | blackhole | node | 71.1% → 66.9% ▼ | 5.00 s → **4.09 s** | 0.0 → 0.0 | 1.65 → **1.06** | 2.80 → **1.17** | 0.0% → 0.0% | 0.0 s → 0.0 s |
| bare | retry storm | python | 26.0% → **41.0%** | 5.00 s → **4.09 s** | 0.0 → 0.0 | 2.48 → **1.07** | 2.77 → **1.11** | 0.0% → 0.0% | 75.0 s → **58.0 s** |
| bare | retry storm | node | 26.0% → **40.9%** | 5.00 s → **4.09 s** | 0.0 → 0.0 | 2.48 → **1.08** | 2.78 → **1.11** | 0.0% → 0.0% | 75.0 s → **58.3 s** |
| bare | thundering herd | python | 23.9% → 18.4% ▼ | 0.97 s → **0.75 s** | 0.0 → 0.0 | 2.64 → **1.02** | — | 0.0% → 0.0% | — |
| bare | thundering herd | node | 23.8% → 18.4% ▼ | 0.96 s → **0.74 s** | 0.0 → 0.0 | 2.64 → **1.02** | — | 0.0% → 0.0% | — |
| bare | noisy neighbour | python | 56.9% → **77.4%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.11 → **1.05** | 0.62 → 0.92 ▼ | 50.8% → **15.9%** | 0.0 s → 0.0 s |
| bare | noisy neighbour | node | 57.0% → **77.4%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.11 → **1.05** | 0.62 → 0.92 ▼ | 50.8% → **15.9%** | 0.0 s → 0.0 s |
| bare | rate limit | python | 86.7% → 86.0% ▼ | 0.26 s → **0.08 s** | 0.0 → 0.0 | 1.28 → **1.04** | — | 0.0% → 0.0% | — |
| bare | rate limit | node | 86.7% → 86.0% ▼ | 0.25 s → **0.08 s** | 0.0 → 0.0 | 1.28 → **1.04** | — | 0.0% → 0.0% | — |
| bare | layered calls | python | 73.5% → 67.8% ▼ | 5.00 s → **4.11 s** | 0.0 → 0.0 | 2.03 → **1.11** | 3.93 → **1.32** | 26.5% → **0.1%** | 0.0 s → 0.0 s |
| bare | layered calls | node | 73.5% → 67.8% ▼ | 5.00 s → **4.01 s** | 0.0 → 0.0 | 2.44 → **1.11** | 5.04 → **1.32** | 76.8% → **0.1%** | 0.0 s → 0.0 s |

### Honour Retry-After (429/503) (`retry_after`): adopted

| base | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |
|---|---|---|---|---|---|---|---|---|---|
| stack | rate limit | python | 86.0% → **96.1%** | 0.08 s → 2.07 s ▼ | 0.0 → 0.0 | 1.04 → 1.33 ▼ | — | 0.0% → 0.0% | — |
| stack | rate limit | node | 86.0% → **96.1%** | 0.08 s → 2.07 s ▼ | 0.0 → 0.0 | 1.04 → 1.33 ▼ | — | 0.0% → 0.0% | — |
| bare | rate limit | python | 86.7% → **96.1%** | 0.26 s → 2.07 s ▼ | 0.0 → 0.0 | 1.28 → 1.33 | — | 0.0% → 0.0% | — |
| bare | rate limit | node | 86.7% → **96.1%** | 0.25 s → 2.07 s ▼ | 0.0 → 0.0 | 1.28 → 1.33 | — | 0.0% → 0.0% | — |

### Retry with backoff and full jitter (`retry`): adopted

| base | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |
|---|---|---|---|---|---|---|---|---|---|
| stack | transient | python | 84.7% → **99.5%** | 2.00 s → 2.22 s ▼ | 0.0 → 0.0 | 1.00 → 1.17 ▼ | — | 0.0% → 0.0% | — |
| stack | transient | node | 84.7% → **99.5%** | 2.00 s → 2.21 s ▼ | 0.0 → 0.0 | 1.00 → 1.17 ▼ | — | 0.0% → 0.0% | — |
| stack | rate limit | python | 85.9% → **96.1%** | 0.07 s → 2.07 s ▼ | 0.0 → 0.0 | 1.00 → 1.33 ▼ | — | 0.0% → 0.0% | — |
| stack | rate limit | node | 85.9% → **96.1%** | 0.07 s → 2.07 s ▼ | 0.0 → 0.0 | 1.00 → 1.33 ▼ | — | 0.0% → 0.0% | — |
| stack | lost answers | python | 83.0% → **99.6%** | 2.00 s → 3.59 s ▼ | 0.0 → 0.0 | 1.00 → 1.20 ▼ | — | 0.0% → 0.0% | — |
| stack | lost answers | node | 83.0% → **99.5%** | 2.00 s → 3.59 s ▼ | 0.0 → 0.0 | 1.00 → 1.20 ▼ | — | 0.0% → 0.0% | — |
| stack | layered calls | python | 65.2% → 64.4% ▼ | 1.52 s → 1.59 s | 0.0 → 0.0 | 0.69 → 0.73 ▼ | 0.11 → 0.23 ▼ | 0.0% → 0.0% | 1.1 s → 1.7 s |
| stack | layered calls | node | 65.2% → 64.4% ▼ | 1.52 s → 1.59 s | 0.0 → 0.0 | 0.69 → 0.73 ▼ | 0.11 → 0.23 ▼ | 0.0% → 0.0% | 1.1 s → 1.7 s |
| bare | transient | python | 84.7% → **99.7%** | 2.00 s → 2.23 s ▼ | 0.0 → 0.0 | 1.00 → 1.17 ▼ | — | 0.0% → 0.0% | — |
| bare | transient | node | 84.7% → **99.7%** | 2.00 s → 2.22 s ▼ | 0.0 → 0.0 | 1.00 → 1.17 ▼ | — | 0.0% → 0.0% | — |
| bare | outage | python | 66.5% → 66.6% | 0.07 s → 0.27 s ▼ | 0.0 → 0.0 | 1.00 → 1.67 ▼ | 1.00 → 2.99 ▼ | 0.0% → 0.0% | 0.0 s → 0.0 s |
| bare | outage | node | 66.5% → 66.6% | 0.07 s → 0.27 s ▼ | 0.0 → 0.0 | 1.00 → 1.67 ▼ | 1.00 → 2.99 ▼ | 0.0% → 0.0% | 0.0 s → 0.0 s |
| bare | blackhole | python | 66.5% → **71.1%** | 2.00 s → 5.00 s ▼ | 0.0 → 0.0 | 1.00 → 1.65 ▼ | 1.00 → 2.79 ▼ | 0.0% → 0.0% | 0.0 s → 0.0 s |
| bare | blackhole | node | 66.5% → **71.1%** | 2.00 s → 5.00 s ▼ | 0.0 → 0.0 | 1.00 → 1.65 ▼ | 1.00 → 2.80 ▼ | 0.0% → 0.0% | 0.0 s → 0.0 s |
| bare | retry storm | python | 66.5% → 26.0% ▼ | 2.00 s → 5.00 s ▼ | 0.0 → 0.0 | 1.00 → 2.48 ▼ | 1.00 → 2.77 ▼ | 0.0% → 0.0% | 26.9 s → 75.0 s ▼ |
| bare | retry storm | node | 66.5% → 26.0% ▼ | 2.00 s → 5.00 s ▼ | 0.0 → 0.0 | 1.00 → 2.48 ▼ | 1.00 → 2.78 ▼ | 0.0% → 0.0% | 27.1 s → 75.0 s ▼ |
| bare | thundering herd | python | 17.5% → **23.9%** | 0.71 s → 0.97 s ▼ | 0.0 → 0.0 | 1.00 → 2.64 ▼ | — | 0.0% → 0.0% | — |
| bare | thundering herd | node | 17.5% → **23.8%** | 0.71 s → 0.96 s ▼ | 0.0 → 0.0 | 1.00 → 2.64 ▼ | — | 0.0% → 0.0% | — |
| bare | noisy neighbour | python | 85.7% → 56.9% ▼ | 2.43 s → 5.00 s ▼ | 0.0 → 0.0 | 1.00 → 1.11 ▼ | 1.00 → **0.62** | 0.0% → 50.8% ▼ | 0.0 s → 0.0 s |
| bare | noisy neighbour | node | 85.7% → 57.0% ▼ | 2.43 s → 5.00 s ▼ | 0.0 → 0.0 | 1.00 → 1.11 ▼ | 1.00 → **0.62** | 0.0% → 50.8% ▼ | 0.0 s → 0.0 s |
| bare | rate limit | python | 85.9% → **86.7%** | 0.07 s → 0.26 s ▼ | 0.0 → 0.0 | 1.00 → 1.28 ▼ | — | 0.0% → 0.0% | — |
| bare | rate limit | node | 85.9% → **86.7%** | 0.07 s → 0.25 s ▼ | 0.0 → 0.0 | 1.00 → 1.28 ▼ | — | 0.0% → 0.0% | — |
| bare | lost answers | python | 83.0% → **99.6%** | 2.00 s → 3.59 s ▼ | 0.0 → 76.5 ▼ | 1.00 → 1.20 ▼ | — | 0.0% → 0.0% | — |
| bare | lost answers | node | 83.0% → **99.6%** | 2.00 s → 3.59 s ▼ | 0.0 → 76.5 ▼ | 1.00 → 1.20 ▼ | — | 0.0% → 0.0% | — |
| bare | layered calls | python | 66.7% → **73.5%** | 1.53 s → 5.00 s ▼ | 0.0 → 0.0 | 1.00 → 2.03 ▼ | 1.00 → 3.93 ▼ | 0.0% → 26.5% ▼ | 0.0 s → 0.0 s |
| bare | layered calls | node | 66.7% → **73.5%** | 1.53 s → 5.00 s ▼ | 0.0 → 0.0 | 1.00 → 2.44 ▼ | 1.00 → 5.04 ▼ | 0.0% → 76.8% ▼ | 0.0 s → 0.0 s |

### Circuit breaker (`circuit_breaker`): adopted

| base | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |
|---|---|---|---|---|---|---|---|---|---|
| stack | outage | python | 66.5% → 65.9% ▼ | 0.09 s → **0.07 s** | 0.0 → 0.0 | 1.06 → **0.67** | 1.18 → **0.04** | 0.0% → 0.0% | 0.0 s → 0.9 s |
| stack | outage | node | 66.5% → 65.9% ▼ | 0.09 s → **0.07 s** | 0.0 → 0.0 | 1.06 → **0.67** | 1.18 → **0.04** | 0.0% → 0.0% | 0.0 s → 0.8 s |
| stack | blackhole | python | 66.9% → 65.9% ▼ | 4.09 s → **2.01 s** | 0.0 → 0.0 | 1.06 → **0.69** | 1.17 → **0.08** | 0.0% → 0.0% | 0.0 s → 1.0 s ▼ |
| stack | blackhole | node | 66.9% → 65.9% ▼ | 4.09 s → **2.01 s** | 0.0 → 0.0 | 1.06 → **0.69** | 1.17 → **0.08** | 0.0% → 0.0% | 0.0 s → 1.0 s ▼ |
| stack | retry storm | python | 41.0% → **88.1%** | 4.09 s → **2.00 s** | 0.0 → 0.0 | 1.07 → **0.92** | 1.11 → **0.44** | 0.0% → 0.0% | 58.0 s → **2.2 s** |
| stack | retry storm | node | 40.9% → **88.1%** | 4.09 s → **2.00 s** | 0.0 → 0.0 | 1.08 → **0.92** | 1.11 → **0.44** | 0.0% → 0.0% | 58.3 s → **2.1 s** |
| stack | thundering herd | python | 18.4% → 17.5% ▼ | 0.75 s → 0.71 s | 0.0 → 0.0 | 1.02 → 1.00 | — | 0.0% → 0.0% | — |
| stack | thundering herd | node | 18.4% → 17.5% ▼ | 0.74 s → 0.71 s | 0.0 → 0.0 | 1.02 → 1.00 | — | 0.0% → 0.0% | — |
| stack | noisy neighbour | python | 86.6% → 85.4% ▼ | 5.00 s → **2.00 s** | 0.0 → 0.0 | 1.05 → **0.87** | 1.04 → **0.74** | 0.0% → 0.0% | 0.0 s → 0.9 s |
| stack | noisy neighbour | node | 86.6% → 85.3% ▼ | 5.00 s → **2.00 s** | 0.0 → 0.0 | 1.05 → **0.87** | 1.04 → **0.74** | 0.0% → 0.0% | 0.0 s → 0.9 s |
| stack | layered calls | python | 67.8% → 64.4% ▼ | 4.33 s → **1.59 s** | 0.0 → 0.0 | 1.11 → **0.73** | 1.32 → **0.23** | 0.0% → 0.0% | 0.0 s → 1.7 s ▼ |
| stack | layered calls | node | 67.9% → 64.4% ▼ | 4.16 s → **1.59 s** | 0.0 → 0.0 | 1.11 → **0.73** | 1.32 → **0.23** | 0.0% → 0.0% | 0.0 s → 1.7 s ▼ |
| bare | outage | python | 66.6% → 65.9% ▼ | 0.27 s → **0.07 s** | 0.0 → 0.0 | 1.67 → **0.67** | 2.99 → **0.04** | 0.0% → 0.0% | 0.0 s → 0.9 s |
| bare | outage | node | 66.6% → 65.9% ▼ | 0.27 s → **0.07 s** | 0.0 → 0.0 | 1.67 → **0.67** | 2.99 → **0.04** | 0.0% → 0.0% | 0.0 s → 0.8 s |
| bare | blackhole | python | 71.1% → 65.9% ▼ | 5.00 s → **2.06 s** | 0.0 → 0.0 | 1.65 → **0.69** | 2.79 → **0.08** | 0.0% → 0.0% | 0.0 s → 1.0 s ▼ |
| bare | blackhole | node | 71.1% → 65.9% ▼ | 5.00 s → **2.06 s** | 0.0 → 0.0 | 1.65 → **0.69** | 2.80 → **0.08** | 0.0% → 0.0% | 0.0 s → 1.0 s ▼ |
| bare | retry storm | python | 26.0% → **88.2%** | 5.00 s → **2.07 s** | 0.0 → 0.0 | 2.48 → **0.92** | 2.77 → **0.44** | 0.0% → 0.0% | 75.0 s → **2.1 s** |
| bare | retry storm | node | 26.0% → **88.1%** | 5.00 s → **2.07 s** | 0.0 → 0.0 | 2.48 → **0.92** | 2.78 → **0.44** | 0.0% → 0.0% | 75.0 s → **2.1 s** |
| bare | thundering herd | python | 23.9% → 17.5% ▼ | 0.97 s → **0.71 s** | 0.0 → 0.0 | 2.64 → **1.00** | — | 0.0% → 0.0% | — |
| bare | thundering herd | node | 23.8% → 17.5% ▼ | 0.96 s → **0.71 s** | 0.0 → 0.0 | 2.64 → **1.00** | — | 0.0% → 0.0% | — |
| bare | noisy neighbour | python | 56.9% → **85.4%** | 5.00 s → **2.02 s** | 0.0 → 0.0 | 1.11 → **0.87** | 0.62 → 0.74 ▼ | 50.8% → **0.0%** | 0.0 s → 0.9 s |
| bare | noisy neighbour | node | 57.0% → **85.3%** | 5.00 s → **2.02 s** | 0.0 → 0.0 | 1.11 → **0.87** | 0.62 → 0.74 ▼ | 50.8% → **0.0%** | 0.0 s → 0.9 s |
| bare | layered calls | python | 73.5% → 65.0% ▼ | 5.00 s → **1.60 s** | 0.0 → 0.0 | 2.03 → **0.81** | 3.93 → **0.47** | 26.5% → **0.0%** | 0.0 s → 1.3 s ▼ |
| bare | layered calls | node | 73.5% → 65.5% ▼ | 5.00 s → **1.60 s** | 0.0 → 0.0 | 2.44 → **0.83** | 5.04 → **0.50** | 76.8% → **0.0%** | 0.0 s → 1.1 s ▼ |

### Adaptive throttling / load shedding (`adaptive_throttle`): opt-in

| base | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |
|---|---|---|---|---|---|---|---|---|---|
| bare | outage | python | 66.6% → 55.0% ▼ | 0.27 s → 0.26 s | 0.0 → 0.0 | 1.67 → **1.26** | 2.99 → **2.12** | 0.0% → 0.0% | 0.0 s → 30.0 s ▼ |
| bare | outage | node | 66.6% → 54.8% ▼ | 0.27 s → 0.26 s | 0.0 → 0.0 | 1.67 → **1.26** | 2.99 → **2.12** | 0.0% → 0.0% | 0.0 s → 30.0 s ▼ |
| bare | blackhole | python | 71.1% → 56.6% ▼ | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.65 → **1.30** | 2.79 → **2.18** | 0.0% → 0.0% | 0.0 s → 30.0 s ▼ |
| bare | blackhole | node | 71.1% → 56.4% ▼ | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.65 → **1.30** | 2.80 → **2.19** | 0.0% → 0.0% | 0.0 s → 30.0 s ▼ |
| bare | retry storm | python | 26.0% → **48.9%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 2.48 → **1.13** | 2.77 → 2.71 | 0.0% → 0.0% | 75.0 s → 75.0 s |
| bare | retry storm | node | 26.0% → **49.3%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 2.48 → **1.13** | 2.78 → 2.72 | 0.0% → 0.0% | 75.0 s → 75.0 s |
| bare | thundering herd | python | 23.9% → 18.0% ▼ | 0.97 s → **0.73 s** | 0.0 → 0.0 | 2.64 → **1.01** | — | 0.0% → 0.0% | — |
| bare | thundering herd | node | 23.8% → 17.9% ▼ | 0.96 s → **0.72 s** | 0.0 → 0.0 | 2.64 → **1.00** | — | 0.0% → 0.0% | — |
| bare | noisy neighbour | python | 56.9% → 55.8% ▼ | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.11 → **1.05** | 0.62 → 0.72 ▼ | 50.8% → **45.2%** | 0.0 s → 7.2 s ▼ |
| bare | noisy neighbour | node | 57.0% → 55.6% ▼ | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.11 → **1.04** | 0.62 → 0.73 ▼ | 50.8% → **45.1%** | 0.0 s → 5.2 s ▼ |
| bare | layered calls | python | 73.5% → 57.5% ▼ | 5.00 s → 5.00 s | 0.0 → 0.0 | 2.03 → **1.58** | 3.93 → **2.89** | 26.5% → **14.6%** | 0.0 s → 20.0 s ▼ |
| bare | layered calls | node | 73.5% → 48.2% ▼ | 5.00 s → 5.00 s | 0.0 → 0.0 | 2.44 → **1.40** | 5.04 → **2.49** | 76.8% → **18.7%** | 0.0 s → 20.0 s ▼ |

### Adaptive concurrency limit (AIMD) (`adaptive_concurrency`): opt-in

| base | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |
|---|---|---|---|---|---|---|---|---|---|
| stack | transient | python | 99.5% → 64.3% ▼ | 2.22 s → 2.14 s | 0.0 → 0.0 | 1.17 → **0.76** | — | 0.0% → 0.0% | — |
| stack | transient | node | 99.5% → 64.3% ▼ | 2.21 s → 2.14 s | 0.0 → 0.0 | 1.17 → **0.76** | — | 0.0% → 0.0% | — |
| stack | blackhole | python | 65.9% → 65.9% | 2.01 s → 2.00 s | 0.0 → 0.0 | 0.69 → 0.67 | 0.08 → **0.03** | 0.0% → 0.0% | 1.0 s → 1.0 s |
| stack | blackhole | node | 65.9% → 65.9% | 2.01 s → 2.00 s | 0.0 → 0.0 | 0.69 → 0.67 | 0.08 → **0.03** | 0.0% → 0.0% | 1.0 s → 1.0 s |
| stack | retry storm | python | 88.1% → **92.3%** | 2.00 s → **1.36 s** | 0.0 → 0.0 | 0.92 → 0.92 | 0.44 → 0.43 | 0.0% → 0.0% | 2.2 s → 1.9 s |
| stack | retry storm | node | 88.1% → **92.3%** | 2.00 s → **1.36 s** | 0.0 → 0.0 | 0.92 → 0.92 | 0.44 → 0.43 | 0.0% → 0.0% | 2.1 s → 1.8 s |
| stack | thundering herd | python | 17.5% → 5.0% ▼ | 0.71 s → **0.21 s** | 0.0 → 0.0 | 1.00 → **0.05** | — | 0.0% → 0.0% | — |
| stack | thundering herd | node | 17.5% → 5.0% ▼ | 0.71 s → **0.21 s** | 0.0 → 0.0 | 1.00 → **0.05** | — | 0.0% → 0.0% | — |
| stack | rate limit | python | 96.1% → 86.7% ▼ | 2.07 s → **0.75 s** | 0.0 → 0.0 | 1.33 → **0.88** | — | 0.0% → 0.0% | — |
| stack | rate limit | node | 96.1% → 86.7% ▼ | 2.07 s → **0.75 s** | 0.0 → 0.0 | 1.33 → **0.88** | — | 0.0% → 0.0% | — |
| stack | lost answers | python | 99.6% → 74.6% ▼ | 3.59 s → **2.59 s** | 0.0 → 0.0 | 1.20 → **0.89** | — | 0.0% → 0.0% | — |
| stack | lost answers | node | 99.5% → 72.7% ▼ | 3.59 s → **2.48 s** | 0.0 → 0.0 | 1.20 → **0.87** | — | 0.0% → 0.0% | — |
| stack | layered calls | python | 64.4% → 64.5% | 1.59 s → 1.58 s | 0.0 → 0.0 | 0.73 → 0.71 | 0.23 → **0.19** | 0.0% → 0.0% | 1.7 s → 1.6 s |
| stack | layered calls | node | 64.4% → 64.5% | 1.59 s → 1.57 s | 0.0 → 0.0 | 0.73 → 0.71 | 0.23 → **0.19** | 0.0% → 0.0% | 1.7 s → 1.6 s |
| bare | transient | python | 99.7% → 64.7% ▼ | 2.23 s → 2.14 s | 0.0 → 0.0 | 1.17 → **0.76** | — | 0.0% → 0.0% | — |
| bare | transient | node | 99.7% → 64.5% ▼ | 2.22 s → 2.14 s | 0.0 → 0.0 | 1.17 → **0.76** | — | 0.0% → 0.0% | — |
| bare | outage | python | 66.6% → 66.3% | 0.27 s → 0.27 s | 0.0 → 0.0 | 1.67 → **1.42** | 2.99 → **2.27** | 0.0% → 0.0% | 0.0 s → 0.9 s |
| bare | outage | node | 66.6% → 66.3% | 0.27 s → 0.26 s | 0.0 → 0.0 | 1.67 → **1.38** | 2.99 → **2.15** | 0.0% → 0.0% | 0.0 s → 0.9 s |
| bare | blackhole | python | 71.1% → 65.6% ▼ | 5.00 s → **2.05 s** | 0.0 → 0.0 | 1.65 → **0.68** | 2.79 → **0.06** | 0.0% → 0.0% | 0.0 s → 1.1 s ▼ |
| bare | blackhole | node | 71.1% → 65.7% ▼ | 5.00 s → **2.05 s** | 0.0 → 0.0 | 1.65 → **0.68** | 2.80 → **0.06** | 0.0% → 0.0% | 0.0 s → 1.1 s ▼ |
| bare | retry storm | python | 26.0% → **92.3%** | 5.00 s → **1.36 s** | 0.0 → 0.0 | 2.48 → **0.92** | 2.77 → **0.43** | 0.0% → 0.0% | 75.0 s → **1.9 s** |
| bare | retry storm | node | 26.0% → **92.3%** | 5.00 s → **1.36 s** | 0.0 → 0.0 | 2.48 → **0.92** | 2.78 → **0.43** | 0.0% → 0.0% | 75.0 s → **1.8 s** |
| bare | thundering herd | python | 23.9% → 5.0% ▼ | 0.97 s → **0.21 s** | 0.0 → 0.0 | 2.64 → **0.05** | — | 0.0% → 0.0% | — |
| bare | thundering herd | node | 23.8% → 5.0% ▼ | 0.96 s → **0.21 s** | 0.0 → 0.0 | 2.64 → **0.05** | — | 0.0% → 0.0% | — |
| bare | noisy neighbour | python | 56.9% → **85.1%** | 5.00 s → **2.05 s** | 0.0 → 0.0 | 1.11 → **0.87** | 0.62 → 0.75 ▼ | 50.8% → **0.0%** | 0.0 s → 1.8 s ▼ |
| bare | noisy neighbour | node | 57.0% → **85.2%** | 5.00 s → **2.04 s** | 0.0 → 0.0 | 1.11 → **0.87** | 0.62 → 0.75 ▼ | 50.8% → **0.0%** | 0.0 s → 1.4 s ▼ |
| bare | rate limit | python | 86.7% → 85.7% ▼ | 0.26 s → **0.10 s** | 0.0 → 0.0 | 1.28 → **0.89** | — | 0.0% → 0.0% | — |
| bare | rate limit | node | 86.7% → 85.7% ▼ | 0.25 s → **0.09 s** | 0.0 → 0.0 | 1.28 → **0.89** | — | 0.0% → 0.0% | — |
| bare | lost answers | python | 99.6% → 74.0% ▼ | 3.59 s → **2.58 s** | 76.5 → **45.8** | 1.20 → **0.89** | — | 0.0% → 0.0% | — |
| bare | lost answers | node | 99.6% → 72.5% ▼ | 3.59 s → **2.49 s** | 76.5 → **44.6** | 1.20 → **0.87** | — | 0.0% → 0.0% | — |
| bare | layered calls | python | 73.5% → 65.2% ▼ | 5.00 s → **1.92 s** | 0.0 → 0.0 | 2.03 → **0.78** | 3.93 → **0.38** | 26.5% → **0.0%** | 0.0 s → 1.6 s ▼ |
| bare | layered calls | node | 73.5% → 65.7% ▼ | 5.00 s → **1.84 s** | 0.0 → 0.0 | 2.44 → **0.80** | 5.04 → **0.41** | 76.8% → **0.0%** | 0.0 s → 1.3 s ▼ |

### Hedged requests (reads) (`hedge`): opt-in

| base | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |
|---|---|---|---|---|---|---|---|---|---|
| stack | healthy | node | 100.0% → 100.0% | 0.07 s → 0.07 s | 0.0 → 0.0 | 1.00 → 1.06 ▼ | — | 0.0% → 0.0% | — |
| stack | transient | python | 99.5% → 98.7% ▼ | 2.22 s → **2.10 s** | 0.0 → 0.0 | 1.17 → 1.20 | — | 0.0% → 0.0% | — |
| stack | transient | node | 99.5% → 98.1% ▼ | 2.21 s → 2.11 s | 0.0 → 0.0 | 1.17 → 1.20 | — | 0.0% → 0.0% | — |
| stack | slow tail | python | 100.0% → 100.0% | 1.56 s → **0.14 s** | 0.0 → 0.0 | 1.00 → 1.09 ▼ | — | 0.0% → 0.0% | — |
| stack | slow tail | node | 100.0% → 100.0% | 1.56 s → **0.14 s** | 0.0 → 0.0 | 1.00 → 1.10 ▼ | — | 0.0% → 0.0% | — |
| stack | outage | node | 65.9% → 65.9% | 0.07 s → 0.07 s | 0.0 → 0.0 | 0.67 → 0.71 ▼ | 0.04 → 0.04 | 0.0% → 0.0% | 0.8 s → 0.8 s |
| stack | blackhole | python | 65.9% → 65.2% ▼ | 2.01 s → 2.07 s | 0.0 → 0.0 | 0.69 → 0.72 ▼ | 0.08 → 0.12 ▼ | 0.0% → 0.0% | 1.0 s → 1.6 s |
| stack | blackhole | node | 65.9% → 65.2% ▼ | 2.01 s → 2.07 s | 0.0 → 0.0 | 0.69 → 0.73 ▼ | 0.08 → 0.12 ▼ | 0.0% → 0.0% | 1.0 s → 1.6 s |
| stack | retry storm | python | 88.1% → 86.3% ▼ | 2.00 s → 2.00 s | 0.0 → 0.0 | 0.92 → 0.96 ▼ | 0.44 → 0.47 ▼ | 0.0% → 0.0% | 2.2 s → 4.1 s ▼ |
| stack | retry storm | node | 88.1% → 86.3% ▼ | 2.00 s → 2.00 s | 0.0 → 0.0 | 0.92 → 0.96 ▼ | 0.44 → 0.47 ▼ | 0.0% → 0.0% | 2.1 s → 4.2 s ▼ |
| stack | noisy neighbour | python | 85.4% → 85.0% | 2.00 s → 2.06 s | 0.0 → 0.0 | 0.87 → 0.92 ▼ | 0.74 → 0.79 ▼ | 0.0% → 0.0% | 0.9 s → 1.6 s |
| stack | noisy neighbour | node | 85.3% → 85.0% | 2.00 s → 2.06 s | 0.0 → 0.0 | 0.87 → 0.92 ▼ | 0.74 → 0.80 ▼ | 0.0% → 0.0% | 0.9 s → 1.6 s |
| stack | rate limit | node | 96.1% → 95.8% | 2.07 s → 2.07 s | 0.0 → 0.0 | 1.33 → 1.40 ▼ | — | 0.0% → 0.0% | — |
| stack | layered calls | python | 64.4% → 64.3% | 1.59 s → 1.64 s | 0.0 → 0.0 | 0.73 → 0.78 ▼ | 0.23 → 0.29 ▼ | 0.0% → 0.0% | 1.7 s → 1.8 s |
| stack | layered calls | node | 64.4% → 64.2% | 1.59 s → 1.63 s | 0.0 → 0.0 | 0.73 → 0.80 ▼ | 0.23 → 0.29 ▼ | 0.0% → 0.0% | 1.7 s → 1.9 s |
| bare | healthy | node | 100.0% → 100.0% | 0.07 s → 0.07 s | 0.0 → 0.0 | 1.00 → 1.06 ▼ | — | 0.0% → 0.0% | — |
| bare | transient | python | 99.7% → 99.9% | 2.23 s → **1.67 s** | 0.0 → 0.0 | 1.17 → 1.22 | — | 0.0% → 0.0% | — |
| bare | transient | node | 99.7% → 99.9% | 2.22 s → **1.67 s** | 0.0 → 0.0 | 1.17 → 1.23 | — | 0.0% → 0.0% | — |
| bare | slow tail | python | 100.0% → 100.0% | 1.56 s → **0.14 s** | 0.0 → 0.0 | 1.00 → 1.09 ▼ | — | 0.0% → 0.0% | — |
| bare | slow tail | node | 100.0% → 100.0% | 1.56 s → **0.14 s** | 0.0 → 0.0 | 1.00 → 1.10 ▼ | — | 0.0% → 0.0% | — |
| bare | blackhole | python | 71.1% → 71.3% | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.65 → 2.61 ▼ | 2.79 → 5.57 ▼ | 0.0% → 0.0% | 0.0 s → 0.0 s |
| bare | blackhole | node | 71.1% → 71.3% | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.65 → 2.62 ▼ | 2.80 → 5.57 ▼ | 0.0% → 0.0% | 0.0 s → 0.0 s |
| bare | retry storm | python | 26.0% → 5.2% ▼ | 5.00 s → 5.00 s | 0.0 → 0.0 | 2.48 → 3.70 ▼ | 2.77 → 3.68 ▼ | 0.0% → 13.8% ▼ | 75.0 s → 75.0 s |
| bare | retry storm | node | 26.0% → 5.2% ▼ | 5.00 s → 5.00 s | 0.0 → 0.0 | 2.48 → 3.68 ▼ | 2.78 → 3.67 ▼ | 0.0% → 13.6% ▼ | 75.0 s → 75.0 s |
| bare | noisy neighbour | python | 56.9% → 56.8% | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.11 → 1.31 ▼ | 0.62 → 0.92 ▼ | 50.8% → 64.7% ▼ | 0.0 s → 0.0 s |
| bare | noisy neighbour | node | 57.0% → 56.9% | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.11 → 1.32 ▼ | 0.62 → 0.92 ▼ | 50.8% → 65.0% ▼ | 0.0 s → 0.0 s |
| bare | lost answers | python | 99.6% → 100.0% | 3.59 s → **2.06 s** | 76.5 → 103.0 ▼ | 1.20 → 1.24 | — | 0.0% → 0.0% | — |
| bare | lost answers | node | 99.6% → 100.0% | 3.59 s → **2.06 s** | 76.5 → 108.5 ▼ | 1.20 → 1.25 | — | 0.0% → 0.0% | — |
| bare | layered calls | python | 73.5% → **74.1%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 2.03 → 5.05 ▼ | 3.93 → 12.88 ▼ | 26.5% → 96.7% ▼ | 0.0 s → 0.0 s |
| bare | layered calls | node | 73.5% → **74.1%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 2.44 → 6.64 ▼ | 5.04 → 17.23 ▼ | 76.8% → 287.2% ▼ | 0.0 s → 0.0 s |

### Attempt timeout (`attempt_timeout`): adopted

| base | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |
|---|---|---|---|---|---|---|---|---|---|
| stack | transient | python | 95.7% → **99.5%** | 4.80 s → **2.22 s** | 0.0 → 0.0 | 1.08 → 1.17 ▼ | — | 0.0% → 0.0% | — |
| stack | transient | node | 95.7% → **99.5%** | 4.80 s → **2.21 s** | 0.0 → 0.0 | 1.08 → 1.17 ▼ | — | 0.0% → 0.0% | — |
| stack | blackhole | python | 60.6% → **65.9%** | 4.80 s → **2.01 s** | 0.0 → 0.0 | 0.66 → 0.69 | 0.17 → **0.08** | 0.0% → 0.0% | 5.8 s → **1.0 s** |
| stack | blackhole | node | 60.6% → **65.9%** | 4.80 s → **2.01 s** | 0.0 → 0.0 | 0.66 → 0.69 | 0.17 → **0.08** | 0.0% → 0.0% | 5.7 s → **1.0 s** |
| stack | retry storm | python | 85.8% → **88.1%** | 4.80 s → **2.00 s** | 0.0 → 0.0 | 0.90 → 0.92 | 0.53 → **0.44** | 0.0% → 0.0% | 5.5 s → **2.2 s** |
| stack | retry storm | node | 85.8% → **88.1%** | 4.80 s → **2.00 s** | 0.0 → 0.0 | 0.90 → 0.92 | 0.53 → **0.44** | 0.0% → 0.0% | 5.6 s → **2.1 s** |
| stack | noisy neighbour | python | 85.0% → 85.4% | 4.80 s → **2.00 s** | 0.0 → 0.0 | 0.86 → 0.87 | 0.74 → 0.74 | 0.0% → 0.0% | 1.8 s → 0.9 s |
| stack | noisy neighbour | node | 85.0% → 85.3% | 4.80 s → **2.00 s** | 0.0 → 0.0 | 0.86 → 0.87 | 0.74 → 0.74 | 0.0% → 0.0% | 1.8 s → 0.9 s |
| stack | lost answers | python | 88.2% → **99.6%** | 4.80 s → **3.59 s** | 0.0 → 0.0 | 1.06 → 1.20 ▼ | — | 0.0% → 0.0% | — |
| stack | lost answers | node | 88.2% → **99.5%** | 4.80 s → **3.59 s** | 0.0 → 0.0 | 1.06 → 1.20 ▼ | — | 0.0% → 0.0% | — |
| stack | layered calls | python | 60.6% → **64.4%** | 4.80 s → **1.59 s** | 0.0 → 0.0 | 0.69 → 0.73 | 0.26 → **0.23** | 0.0% → 0.0% | 4.0 s → **1.7 s** |
| stack | layered calls | node | 60.6% → **64.4%** | 4.80 s → **1.59 s** | 0.0 → 0.0 | 0.69 → 0.73 | 0.26 → **0.23** | 0.0% → 0.0% | 4.0 s → **1.7 s** |
| bare | transient | python | 95.7% → **99.7%** | 5.00 s → **2.23 s** | 0.0 → 0.0 | 1.13 → 1.17 | — | 4.7% → **0.0%** | — |
| bare | transient | node | 95.7% → **99.7%** | 5.00 s → **2.22 s** | 0.0 → 0.0 | 1.13 → 1.17 | — | 4.7% → **0.0%** | — |
| bare | blackhole | python | 66.5% → **71.1%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.34 → 1.65 ▼ | 1.00 → 2.79 ▼ | 33.5% → **0.0%** | 0.0 s → 0.0 s |
| bare | blackhole | node | 66.5% → **71.1%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.34 → 1.65 ▼ | 1.00 → 2.80 ▼ | 33.5% → **0.0%** | 0.0 s → 0.0 s |
| bare | retry storm | python | 80.7% → 26.0% ▼ | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.25 → 2.48 ▼ | 1.71 → 2.77 ▼ | 0.0% → 0.0% | 12.3 s → 75.0 s ▼ |
| bare | retry storm | node | 80.7% → 26.0% ▼ | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.25 → 2.48 ▼ | 1.71 → 2.78 ▼ | 0.0% → 0.0% | 12.3 s → 75.0 s ▼ |
| bare | noisy neighbour | python | 27.2% → **56.9%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.01 → 1.11 ▼ | 0.06 → 0.62 ▼ | 72.8% → **50.8%** | 20.0 s → **0.0 s** |
| bare | noisy neighbour | node | 27.2% → **57.0%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.01 → 1.11 ▼ | 0.06 → 0.62 ▼ | 72.8% → **50.8%** | 20.0 s → **0.0 s** |
| bare | lost answers | python | 88.2% → **99.6%** | 5.00 s → **3.59 s** | 76.5 → 76.5 | 1.20 → 1.20 | — | 13.5% → **0.0%** | — |
| bare | lost answers | node | 88.2% → **99.6%** | 5.00 s → **3.59 s** | 76.5 → 76.5 | 1.20 → 1.20 | — | 13.5% → **0.0%** | — |
| bare | layered calls | python | 66.7% → **73.5%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.17 → 2.03 ▼ | 1.00 → 3.93 ▼ | 33.3% → **26.5%** | 0.0 s → 0.0 s |
| bare | layered calls | node | 66.7% → **73.5%** | 5.00 s → 5.00 s | 0.0 → 0.0 | 1.17 → 2.44 ▼ | 1.00 → 5.04 ▼ | 33.3% → 76.8% ▼ | 0.0 s → 0.0 s |

### Full jitter (vs none) (`jitter`): adopted

| base | scenario | runtime | availability | p99 | dup. writes | attempts per call | … in the outage | wasted | recovery |
|---|---|---|---|---|---|---|---|---|---|
| stack | transient | python | 99.5% → 99.5% | 2.36 s → **2.22 s** | 0.0 → 0.0 | 1.17 → 1.17 | — | 0.0% → 0.0% | — |
| stack | transient | node | 99.5% → 99.5% | 2.36 s → **2.21 s** | 0.0 → 0.0 | 1.17 → 1.17 | — | 0.0% → 0.0% | — |
| stack | outage | python | 66.0% → 65.9% | 0.10 s → **0.07 s** | 0.0 → 0.0 | 0.67 → 0.67 | 0.04 → 0.04 | 0.0% → 0.0% | 0.9 s → 0.9 s |
| stack | outage | node | 66.0% → 65.9% | 0.10 s → **0.07 s** | 0.0 → 0.0 | 0.67 → 0.67 | 0.04 → 0.04 | 0.0% → 0.0% | 0.8 s → 0.8 s |
| bare | transient | python | 99.7% → 99.7% | 2.36 s → **2.23 s** | 0.0 → 0.0 | 1.17 → 1.17 | — | 0.0% → 0.0% | — |
| bare | transient | node | 99.7% → 99.7% | 2.36 s → **2.22 s** | 0.0 → 0.0 | 1.17 → 1.17 | — | 0.0% → 0.0% | — |
| bare | outage | python | 66.8% → 66.6% | 0.31 s → **0.27 s** | 0.0 → 0.0 | 1.67 → 1.67 | 2.99 → 2.99 | 0.0% → 0.0% | 0.0 s → 0.0 s |
| bare | outage | node | 66.8% → 66.6% | 0.31 s → **0.27 s** | 0.0 → 0.0 | 1.67 → 1.67 | 2.99 → 2.99 | 0.0% → 0.0% | 0.0 s → 0.0 s |
| bare | rate limit | python | 87.3% → 86.7% ▼ | 0.32 s → **0.26 s** | 0.0 → 0.0 | 1.28 → 1.28 | — | 0.0% → 0.0% | — |
| bare | rate limit | node | 87.3% → 86.7% ▼ | 0.32 s → **0.25 s** | 0.0 → 0.0 | 1.28 → 1.28 | — | 0.0% → 0.0% | — |
