# Harden `orders.charge`

You are hardening one async JavaScript function (a Node.js ES module) for **reliability, availability and serviceability**.
Edit **only `hardened.mjs` in this folder**. Keep the function name `charge` and its signature.
When you are done, `ras-anything verify orders.charge` must print PASS. Run it yourself (it works from this
folder) and iterate until it passes. Do not enable anything; do not edit the original source,
`calls.json` or `meta.json`.

## The function

`orders.charge` in `orders.mjs` line 30. Recorded **12 real calls** from
`node app.mjs`. Dependency requests it made:
- 12× `POST http://127.0.0.1:40393/v1/charges`

What the scan found:
- `catch {}` swallows errors
- retries a POST with no Idempotency-Key
- no logging on the failure path
- under seeded chaos the app failed in 1/1 runs where this function's dependency calls were faulted

```js
const api = () => process.env.SHOP_API_URL ?? "https://api.shop.example";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const charge = async (order, amountCents) => {
  for (let attempt = 0; attempt < 3; attempt++) { // the provider is flaky, so try a few times
    try {
      const res = await fetch(`${api()}/v1/charges`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ order: order.id, amount_cents: amountCents, currency: "eur" }),
        signal: AbortSignal.timeout(10_000),
      });
      if (res.ok) return await res.json();
    } catch {}
    await sleep(300);
  }
  return null;
};
```

How callers use the result (a fallback must be something they already handle):

```js
// app.mjs:24
  const total = goods + (quote?.cents ?? 0);
  const shipping = quote ? eur(quote.cents) : " pickup";
  const record = await charge(order, total);
  if (!record) {
    console.log(`  ${order.id}  ${eur(goods)} + ${shipping}  PENDING (payment failed, retry tomorrow)`);
    continue;
  }
```

Recorded examples:
- `charge({'id': 'ORD-2001', 'name': 'A. Kaya', 'country': 'DE', 'sku': 'TEE-M', 'qty': 1}, 2390)` → {'id': 'ch_0001', 'order': 'ORD-2001', 'amount_cents': 2390, 'status': 'succeeded'}
  dependency I/O: POST http://127.0.0.1:40393/v1/charges -> 200
- `charge({'id': 'ORD-2002', 'name': 'B. Lin', 'country': 'FR', 'sku': 'PEN-3', 'qty': 2}, 2120)` → {'id': 'ch_0002', 'order': 'ORD-2002', 'amount_cents': 2120, 'status': 'succeeded'}
  dependency I/O: POST http://127.0.0.1:40393/v1/charges -> 200
- `charge({'id': 'ORD-2003', 'name': 'C. Moreau', 'country': 'TW', 'sku': 'BAG-XL', 'qty'…, 9560)` → {'id': 'ch_0003', 'order': 'ORD-2003', 'amount_cents': 9560, 'status': 'succeeded'}
  dependency I/O: POST http://127.0.0.1:40393/v1/charges -> 200

## The contract (what `verify` enforces)

1. **Same happy path.** With healthy dependencies, return exactly what the original returned for
   every recorded call (compared with `util.isDeepStrictEqual`). Make the same requests (same method, URL, body); you
   may add headers (e.g. `Idempotency-Key`) and must pass a timeout (`signal` / `timeout`). Where the original raised,
   raise the same exception type (or a subclass).
2. **Bounded latency: 5 s per call**, everything included: timeouts, backoff sleeps,
   Retry-After waits. Every boundary call needs a timeout (`fetch(url, { signal })`,
   `http.get(url, { signal })`, `execFile(cmd, args, { timeout })`, `execFileSync(cmd, args, { timeout })`); a call with no timeout that
   hangs is a hard fail. Budget the attempts so worst case stays under 5 s.
3. **Retry only transient failures**: timeouts, connection errors, HTTP 408/425/429/500/502/503/504
   (honour `Retry-After`), EAGAIN, a truncated/unparsable body from a 2xx, a child process exiting 75 or killed by its timeout. Exponential
   backoff with full jitter. **Never retry other 4xx** or programming errors.
4. **Idempotency.** Never send a POST/PUT/PATCH/DELETE twice unless every attempt carries the same
   `Idempotency-Key` header, derived deterministically from the logical operation (e.g. the invoice
   id), not random per attempt. A timed-out POST may already have taken effect.
5. **Degrade explicitly, never silently.** When the dependency stays down, either raise a structured
   error (`throw new RasError("...", { cause: e, context: {...} })`) or return the declared fallback.
   Declare it as the export `RAS_FALLBACK` (a value, or a function taking the same arguments;
   to declare `null` or `undefined` as the fallback use `() => null`).
   Any other value that differs from the happy result counts as **silent wrong data** and fails.
   Swallowing errors (`catch { return null }`, `.catch(() => null)`) is the defect you are fixing.
   A fallback counts only in a call where a dependency request really failed and never got a good
   answer (the recorder watches the boundary; a log line alone proves nothing). An open
   `CircuitBreaker` makes no request, so raise then.
6. **Serviceable errors.** Log each give-up with `logEvent("...", { ...context })` (JSON line on stderr,
   with a trace id) and chain causes (`{ cause: e }`).

`verify` replays every recorded call against recorded dependency answers (no network), on a
**virtual clock** (sleeps and timeouts cost no real time and are measured exactly), then replays them
again under this fault matrix, for the original and for your version, with the same seeds:

| fault | what happens at the boundary |
|---|---|
| `timeout` | no answer: caller's timeout fires; with no timeout it hangs (POST/PUT still reach the server) |
| `refused` | connection refused |
| `http5xx` | HTTP 503 (subprocess: exit 75 EX_TEMPFAIL) |
| `http429` | HTTP 429 with Retry-After: 1 |
| `slow` | +3 s latency, then the correct answer |
| `truncated` | body (or stdout) cut in half |
| `outage` | dependency down (refused) for 3 consecutive attempts, then recovers |
| `mixed` | seeded mix: each attempt faults with p=0.3, kind drawn uniformly |

Gates: happy replay 100% equal · no `wrong` results · no hangs · p99 latency ≤ 5 s ·
duplicate non-idempotent side effects ≤ original · availability (ok + degraded) strictly higher
than the original.

## Helpers (`./_runtime.cjs`, Node built-ins only; times are in milliseconds)

- `await retry(fn, {attempts = 3, baseMs = 100, capMs = 2000, timeoutMs, budgetMs, retryOn = isTransient})`: calls
  `fn({attempt, signal})`; full-jitter backoff, honours Retry-After. `timeoutMs` bounds each attempt (pass `signal` on),
  `budgetMs` the whole (attempts are cut to what is left). Rethrows the last error unchanged.
- `retrySync(fn, {...same})`: for synchronous functions; `fn({attempt, timeoutMs})`, give `timeoutMs` to the call.
- `await raiseForStatus(res)`: fetch doesn't reject on 4xx/5xx. Returns `res` when status < 400, else throws an `HttpError`
  (`.status`, `.headers`, `.url`, `.body`), which `isTransient` and `retry` understand.
- `isTransient(err)`: the transient rule from point 3 (not unparsable bodies: add `SyntaxError` yourself when a 2xx body
  fails to parse, e.g. `retryOn: (e) => isTransient(e) || e instanceof SyntaxError`).
- `await withTimeout((signal) => ..., ms)`: for anything; rejects with a `TimeoutError`.
- `new CircuitBreaker({failThreshold, cooldownMs})`: `await .call(fn, ...args)`; throws `CircuitOpenError` while open.
- `new RasError(message, {cause, context})`: structured error with a trace id. `logEvent(event, {...})`: one JSON line on stderr.
- `scope("<id>")`: the module-level names listed in the stub, live from the original module.

`hardened.mjs` must stay loadable with `require()`: no top-level `await`. Keep the imports' relative paths as they are in the
stub (they count from `_ras_hardened/`). Harness modules (`node:module`, `node:vm`, `node:worker_threads`, ...) are refused.
In a body that arrives cut in half, `JSON.parse` / `res.json()` throws a `SyntaxError`.
A timed-out `fetch` keeps no promise pending: always pass the `signal`; a promise that never settles is a hang.

Write it like production code: small, readable, commented where it matters. Then run
`ras-anything verify orders.charge` and fix until it passes.
