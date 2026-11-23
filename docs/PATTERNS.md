# Resilience patterns: what is in, what isn't, and the numbers

ras-anything's runtime (`ras_anything.runtime` for Python, `_runtime.cjs` for Node.js) has twelve resilience patterns,
each a plugin module of its own with the same name, parameters and behaviour in both languages. Each was measured ON
vs OFF on a virtual clock, in both runtimes. **Adopted** ones are on by default in a `Policy` and are the only ones
`TASK.md` recommends to coding agents. **Opt-in** ones ship, but you switch them on yourself.

The tables on this page are generated from [`examples/patterns-lab/results.json`](../examples/patterns-lab/results.json)
(a test compares them). Reproduce them with:

```sh
ras-anything bench --patterns --seeds 20 --out examples/patterns-lab     # ~7 minutes on 8 CPUs, no network
```

## Use

```python
from ras_anything.runtime import Policy, RejectedError, idempotency_key

shop = Policy("shop-api", deadline={"timeout_s": 4.7}, attempt_timeout={"timeout_s": 3.4}, retry={"attempts": 4})   # module level

shop.call(lambda a: urlopen(url, timeout=a.timeout_s).read())                                  # a read
shop.call(post, method="POST", idempotency_key=idempotency_key("charge", order_id, cents))     # a write: post(a) sends a.headers
await shop.acall(fn)                                                                           # async: attempt timeouts are enforced
```

```js
const shop = new Policy("shop-api", { deadline: { timeoutMs: 4700 }, attemptTimeout: { timeoutMs: 3400 }, retry: { attempts: 4 } });
await shop.call(async ({ signal, headers }) => (await raiseForStatus(await fetch(url, { signal, headers }))).json());
shop.callSync(({ timeoutMs }) => execFileSync(cmd, args, { timeout: timeoutMs }), { method: "RUN" });
```

A keyword named after a pattern switches it (`True`/`False`) or sets its parameters (a dict). Parameters are seconds in
Python (`cooldown_s`) and milliseconds in Node (`cooldownMs`). Order, outermost first (Polly v8's standard pipeline,
extended):

> fallback → deadline → bulkhead → idempotency → retry budget → Retry-After → **retry** → circuit breaker →
> adaptive throttle → adaptive concurrency → hedge → attempt timeout → *your function*

The parts are also there on their own: `retry`/`aretry`/`retrySync`, `RetryBudget`, `CircuitBreaker`, `Bulkhead`,
`AdaptiveThrottle`, `AdaptiveConcurrency`, `hedge`/`ahedge`, `StaleCache`, `deadline`/`withDeadline`,
`attempt_timeout`/`attemptTimeout`, `idempotency_key`/`idempotencyKey`.

## Measured: each pattern in the scenario it is meant for

`off → on`, mean over the seeds. **Bold** = clearly better, ▼ = clearly worse. *In the stack*: added to (or removed
from) all other adopted patterns. *Bare client*: added to an attempt timeout and three attempts; shown where another
pattern of the stack already does the job.

<!-- lab:summary -->
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
<!-- /lab -->

## Verdicts

<!-- lab:reasons -->
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
<!-- /lab -->

Every clear difference in every scenario: [`RESULTS.md`](../examples/patterns-lab/RESULTS.md).

### What the numbers say

- **Retries alone make overload worse.** In the retry storm (85 calls/s, server good for 100/s, 3x slower for 15 s) the
  bare client never recovers within the run. The breaker is what ends the storm.
- **The breaker's price** is a second or two of calls after the dependency is back (cooldown + one probe), most visible
  in `layered calls`, where two breakers sit on top of each other.
- **Counting 429 as an outage was a mistake** we made and measured: the breaker opened on rate limits. Breakers ignore
  429 now; throttles and concurrency limits count it.
- **Retries the server asked for** (`Retry-After`) don't take a retry-budget token, else the budget defeats Retry-After.
- **Deadlines and retry budgets look idle in the stack** because the breaker ends the same trouble first; against the
  bare client they are what stops work nobody waits for, and what bounds amplification before a breaker opens.
  In Node, abandoned async work isn't cancelled by itself: without a deadline the bare client wastes more there than
  in Python.
- **Adaptive throttling** does its job alone but adds nothing clear next to a breaker. On low-traffic functions it
  also sheds calls the dependency would have answered (see the example apps below, where verify rejects it).
- **Bulkhead and concurrency limits** need a size that fits the traffic. The default of 10 sheds good calls in the
  bursts here. In `noisy neighbour`, the case it is for, p99 drops sharply.
- **Hedging** fixes the slow tail and costs extra load; with hedges drawing from the retry budget, real retries lose.
- **Full jitter** shortens p99 a little when many calls retry. In `thundering herd` it made no clear difference.

## The example apps

Recorded calls of four example functions, replayed by `verify` (fault matrix, then the outage sequence) with a
hardening built on a `Policy` (`examples/patterns-lab/hardened/`): the whole stack, then with one pattern switched.
Bold = differs from the stack. Per-call trials start with fresh state, so breakers and budgets show in the outage
columns. `verify` ✗ names the gates that fail.

<!-- lab:examples -->
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
<!-- /lab -->

## The bare client → the stack, in every scenario

<!-- lab:base -->
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
<!-- /lab -->

`availability` here means an answer within the caller's patience (5 s). In an outage with no fallback it can only drop:
failing fast (p99, load) is what the stack buys. With `fallback` on, see above.

## The catalog

### Attempt timeout (`attempt_timeout`): adopted
One attempt gets `timeout_s` (3.5), cut to what is left of the deadline. Async code is interrupted; blocking code must
pass `attempt.timeout_s` to the call. An attempt that ran out of time with less than half its timeout left by the
deadline raises `DeadlineExceeded` and doesn't count against the dependency.
*Release It! 2e: Timeouts · AWS Builders' Library, Timeouts, retries, and backoff with jitter · AWS REL05-BP05 Set client
timeouts · Polly v8, resilience4j TimeLimiter, Failsafe: Timeout.*

### Total deadline with propagation (`deadline`): adopted
The whole call ends `timeout_s` (4.8) from now, or at the caller's own deadline if that is earlier. It travels with the
context (`contextvars` / `AsyncLocalStorage`) to nested policies, `retry`, `attempt_timeout`, `withTimeout`. No wait or
attempt crosses it, and nothing is sent once it has passed. A deadline can't interrupt a blocking call by itself; verify
doesn't pretend it can.
*Google SRE ch. 22: deadline propagation · Release It! 2e: Fail Fast · Polly v8: total request timeout.*

### Retry with full jitter (`retry`): adopted
`attempts` (3), wait `random(0, min(cap, base·2ⁿ))`, transient errors only (timeouts, connection errors,
408/425/429/5xx-transient), never a write without an idempotency key, never a rejection of our own.
*AWS Architecture Blog: Exponential Backoff And Jitter · AWS Builders' Library · Google SRE ch. 21: at most three attempts
per request · AWS REL05-BP03 Control and limit retry calls · resilience4j, Polly v8, Failsafe: Retry.*

### Retry budget (`retry_budget`): adopted
A token bucket per policy: each call adds `ratio` (0.1), each second `min_per_s` (2), at most `burst` (10); a retry or
hedge takes one. So retries ≤ burst + ratio × calls + min_per_s × seconds, whatever happens (tested). Retries the server
scheduled with Retry-After are free.
*Google SRE ch. 21: per-client retry budget (10%) · AWS Builders' Library: token bucket for retries · Finagle RetryBudget
(20% + 10 per second).*

### Retry-After (`retry_after`): adopted
Waits at least as long as a 429/503 asked (seconds or HTTP date); gives up at once when that is past the deadline.
*RFC 9110 §10.2.3 · AWS REL05-BP02 Throttle requests.*

### Circuit breaker (`circuit_breaker`): adopted
Opens after `fail_threshold` (5) consecutive failures or when `failure_rate` (50%) of the last `window` (50) calls failed
with at least `min_calls` (20); after `cooldown_s` (1) up to `half_open_probes` (1) calls get through: that many successes
close it, one failure re-opens it. 429s, other 4xx and bugs don't count; outcomes of calls admitted before the last
change of state are ignored. Open = `CircuitOpenError`, not retried.
*Release It! 2e: Circuit Breaker · resilience4j (window 100, 50%, 10 half-open calls, 60 s) · Polly v8 (10% of ≥100 in 30 s,
break 5 s) · AWS REL05-BP01 graceful degradation. Our cooldown is short because a probe is one call.*

### Idempotency-Key (`idempotency`): adopted
One key per logical operation, sent by every attempt, is what lets a write be repeated at all. `idempotency_key(op,
*parts)` derives it from what identifies the operation: the same in every attempt, process and language, UUID-shaped. The
header is a structured-field string (`"…"`), as the draft requires; `quoted: False` sends it bare. 409 (the first request
is still running) is retried with the same key; 422 (key reused for another payload) never.
*IETF draft-ietf-httpapi-idempotency-key-header-09 · AWS Builders' Library: Making retries safe with idempotent APIs.*

### Fallback hierarchy (`fallback`): adopted
Fresh answer → the last good one for this `cache_key`, at most `stale_ttl_s` (60) old → the declared `degraded` value →
the error. The dependency is always asked first. Every step down is logged. Bugs and permanent errors pass through.
Adopted on availability with declared or stale answers; *fresh* availability doesn't change.
*AWS REL05-BP01 · Google SRE ch. 22: degraded results · RFC 5861 stale-if-error · Polly v8, Failsafe: Fallback.*

### Bulkhead (`bulkhead`): opt-in
At most `max_concurrent` (10) calls into one dependency; full = `BulkheadFullError` at once, no queue.
*Release It! 2e: Bulkheads · resilience4j semaphore bulkhead · Polly v8 concurrency limiter · AWS REL05-BP04 Fail fast and
limit queues.* Use it when one app serves several dependencies from shared slots, sized above the normal concurrency
(calls per second × seconds per call).

### Adaptive throttling / load shedding (`adaptive_throttle`): opt-in
Rejects locally with probability `max(0, (requests − K·accepts) / (requests + 1))` over the last `window_s` (120), K = 2.
A request counts when its outcome is known, so a burst still in flight isn't mistaken for rejections.
*Google SRE ch. 21: client-side throttling · AWS Builders' Library: Using load shedding to avoid overload · Release It! 2e:
Shed Load, Create Back Pressure.*

### Adaptive concurrency (`adaptive_concurrency`): opt-in
An AIMD limit on calls in flight: +1 per round of successes while in use, ×0.7 per round with failures.
*Netflix concurrency-limits · Google SRE ch. 22 · Release It! 2e: Governor.* It reads every failure as congestion, so random
failures starve it (see `transient`).

### Hedged requests (`hedge`): opt-in
Reads only: a second attempt after the p95 of recent answers (`delay_s` until 20 are known), the first answer wins, the
loser is cancelled (async) or told (`attempt.cancelled`). Hedges take retry-budget tokens.
*Dean & Barroso, The Tail at Scale · Polly v8 hedging · gRPC request hedging.*

## How it is measured

**Scenarios** (`ras_anything/lab.py`): simulated workloads with many concurrent calls, seeded arrivals and faults, a
dependency with workers and a queue that keeps working on requests nobody waits for any more. The calls go through the
real `Policy` code: Python on an asyncio loop and Node on timers, both on a virtual clock (`agents/python/lab.py`,
`lang/node_agent/lab.cjs`; the same simulation, checked against each other). Every variant of a pattern meets the same
arrivals and the same faults. A worker gets `{"runs": [{"scenario", "patterns", "seed"}], "out"}` and reports each call's
outcome and latency and each attempt's arrival time; all arithmetic is in `lab.py`.

**Metrics**: availability (answered within 5 s), p99 over all calls, duplicate writes (one operation done under several
keys), **retry amplification** (attempts per logical call; per layer where calls are layered; also inside the outage
window), attempts for callers that had left (`wasted`), and recovery (seconds after the trouble until 95% of the calls
of two seconds get fresh answers).

**Rule** (`lab.decide`): a difference is *clear* when the mean over the seeds moves by more than the margin (0.5 points of
availability, 5% of p99 or amplification, 1 duplicate, 1 s of recovery, 2 points of wasted) and at least 80% of the seeds agree.
*Rejected*: nothing got clearly better. *Adopted*: something did (a pattern outside the standard stack must improve the
stack itself), and in the stack: the healthy scenario is unchanged, no duplicates, no scenario lost more than 5 points of
availability or needed 2 s longer to recover, availability did not fall by more than 1 point over all scenarios added
up, and p99 or load only rose where availability rose somewhere. *Opt-in*: the rest. When the runtimes disagree, the weaker
verdict counts.

**Honesty**: the defaults (breaker cooldown, budget trickle, what counts as a failure) and the limits in the rule were
settled on other seeds (1–5 and 101–120) while looking at results. Ours is one reading of one set of simulated
workloads: sizes and rates are in `SCENARIOS`, and a real workload can differ. Earlier rules and defaults gave other verdicts
(with the first ones the breaker and the retry budget came out opt-in). The published seeds, 1001–1020, were not used for
tuning; one earlier full run on them fixed a timing bug in the Node example replay and changed no parameter.

## Adding a pattern

A module in `ras_anything/patterns/` with a `PATTERN` and a `Layer`, its name in `patterns.NAMES`, and the same in
`lang/node_agent/patterns/`. Contract tests (`tests/test_patterns.py`, `tests/test_patterns_parity.py`) cover it: alone it
passes results and errors through, any module can be missing, same registry in both languages. Then
`ras-anything bench --patterns` gives its verdict, and a test fails until the module states it.

## Structured errors (RFC 9457) and human gates (AX)

Hardened code and agent callers share a machine-readable failure contract (`ras_anything/patterns/problem.py` and `ras_anything/lang/node_agent/patterns/problem.cjs`), designed so an upstream agent can decide its next step without scraping raw tracebacks or seeing leaked secrets:

- **`RasError(message, cause=..., dependency=..., context=...)`** (Python) / **`new RasError(message, { cause, dependency, context })`** (Node) builds a `.problem` dict aligned with RFC 9457:
  - `type`: `urn:ras-anything:problem:<cause>` (10 causes: `timeout`, `unavailable`, `rate_limited`, `quota`, `invalid_request`, `auth`, `conflict`, `dependency_bug`, `approval_required`, `unknown`)
  - `title`, `status`, `detail` (PII/secret/path-masked via `mask()`), `retryable`, `retry_after_s`, `next_action` (`retry_later` | `change_params` | `use_fallback` | `escalate` | `stop`), `trace_id`, `dependency`, `fault_class`, `needs_human_authorization`
- **`require_approval(action, target, amount=None)`** / **`requireApproval(action, target, { amount })`** checks `.ras/approvals.json` (or `RAS_ANYTHING_APPROVALS`). When an irreversible operation lacks explicit authorization, it raises `ApprovalRequired` with `next_action: "escalate"` and `needs_human_authorization: true`.
- **Measured AX benchmark** (`ras_anything/ax.py`, 10-cause catalog): deterministic policy-agent next-action accuracy rose from **30.0%** (2.9 mean steps, 40 PII/secret/internal leaks) on plain tracebacks to **100.0%** (1.0 mean step, **0** leaks) on `RasError.problem`.
