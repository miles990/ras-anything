# Harden `orders.printLabel`

You are hardening one async JavaScript function (a Node.js ES module) for **reliability, availability and serviceability**.
Edit **only `hardened.mjs` in this folder**. Keep the function name `printLabel` and its signature.
When you are done, `ras-anything verify orders.printLabel` must print PASS. Run it yourself (it works from this
folder) and iterate until it passes. Do not enable anything; do not edit the original source,
`calls.json` or `meta.json`.

## The function

`orders.printLabel` in `orders.mjs` line 47. Recorded **12 real calls** from
`node app.mjs`. Dependency requests it made:
- 1× `RUN node label.mjs {"id":"ORD-2001","to":"A. Kaya","country":"DE","charge":"ch_0001"`
- 1× `RUN node label.mjs {"id":"ORD-2002","to":"B. Lin","country":"FR","charge":"ch_0002"}`
- 1× `RUN node label.mjs {"id":"ORD-2003","to":"C. Moreau","country":"TW","charge":"ch_000`
- 1× `RUN node label.mjs {"id":"ORD-2004","to":"D. Novak","country":"US","charge":"ch_0004`
- 1× `RUN node label.mjs {"id":"ORD-2005","to":"A. Kaya","country":"IS","charge":"ch_0005"`
- 1× `RUN node label.mjs {"id":"ORD-2006","to":"B. Lin","country":"DE","charge":"ch_0006"}`
- 1× `RUN node label.mjs {"id":"ORD-2007","to":"C. Moreau","country":"FR","charge":"ch_000`
- 1× `RUN node label.mjs {"id":"ORD-2008","to":"D. Novak","country":"TW","charge":"ch_0008`
- 1× `RUN node label.mjs {"id":"ORD-2009","to":"A. Kaya","country":"US","charge":"ch_0009"`
- 1× `RUN node label.mjs {"id":"ORD-2010","to":"B. Lin","country":"IS","charge":"ch_0010"}`
- 1× `RUN node label.mjs {"id":"ORD-2011","to":"C. Moreau","country":"DE","charge":"ch_001`
- 1× `RUN node label.mjs {"id":"ORD-2012","to":"D. Novak","country":"FR","charge":"ch_0012`

What the scan found:
- execFile() without timeout
- no retry on transient errors
- no logging on the failure path
- under seeded chaos the app failed in 2/2 runs where this function's dependency calls were faulted

```js
async function printLabel(order, chargeId) {
  const { stdout } = await run(process.execPath, [LABEL, JSON.stringify({ id: order.id, to: order.name, country: order.country, charge: chargeId })]);
  return JSON.parse(stdout);
}
```

How callers use the result (a fallback must be something they already handle):

```js
// app.mjs:30
  }
  paid++;
  const label = await printLabel(order, record.id);
  console.log(`  ${order.id}  ${eur(goods)} + ${shipping}  paid ${record.id}  label ${label.bytes}B ${label.sha256.slice(0, 8)}`);
}
const ledger = await (await fetch(`${upstream.url}/v1/ledger`, { signal: AbortSignal.timeout(5000) })).json();
console.log(`${paid}/${orders.length} paid · ledger: ${ledger.charges} charges, ${ledger.duplicates} duplicate, total${eur(ledger.total_cents)} EUR`);
```

Recorded examples:
- `printLabel({'id': 'ORD-2001', 'name': 'A. Kaya', 'country': 'DE', 'sku': 'TEE-M', 'qty': 1}, 'ch_0001')` → {'bytes': 72, 'sha256': '26d890fbde1cb224ee8f920ab83e6f8789fb8632f1a9339be6fa5ec06c86aa2a'}
  dependency I/O: RUN node label.mjs {"id":"ORD-2001","to":"A. Kaya","country":"DE","charge":"ch_0001"} -> 0
- `printLabel({'id': 'ORD-2002', 'name': 'B. Lin', 'country': 'FR', 'sku': 'PEN-3', 'qty': 2}, 'ch_0002')` → {'bytes': 71, 'sha256': '3ae963e52011af90f82a7f357128ee8ebdb6eed8f8ebc6094689d41ae468abac'}
  dependency I/O: RUN node label.mjs {"id":"ORD-2002","to":"B. Lin","country":"FR","charge":"ch_0002"} -> 0
- `printLabel({'id': 'ORD-2003', 'name': 'C. Moreau', 'country': 'TW', 'sku': 'BAG-XL', 'qty'…, 'ch_0003')` → {'bytes': 74, 'sha256': '217a3d1e2b002fe14e09243cff2637f8ecce4827b6d3a6fd490dc006ea60d0a7'}
  dependency I/O: RUN node label.mjs {"id":"ORD-2003","to":"C. Moreau","country":"TW","charge":"ch_0003"} -> 0

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
`ras-anything verify orders.printLabel` and fix until it passes.
