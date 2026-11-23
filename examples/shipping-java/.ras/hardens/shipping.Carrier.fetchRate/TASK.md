# Harden `shipping.Carrier.fetchRate`

You are hardening one Java static method for **reliability, availability and serviceability**.
Edit **only `hardened.java` in this folder**. Keep the function name `fetchRate` and its signature.
When you are done, `ras-anything verify shipping.Carrier.fetchRate` must print PASS. Run it yourself (it works from this
folder) and iterate until it passes. Do not enable anything; do not edit the original source,
`calls.json` or `meta.json`.

## The function

`shipping.Carrier.fetchRate` in `src/shipping/Carrier.java` line 17. Recorded **6 real calls** from
`java -cp out shipping.Main`. Dependency requests it made:
- 6× `GET http://127.0.0.1:46397/v1/rates?…`

What the scan found:
- no logging on the failure path
- no retry on transient errors
- HttpClient.send() without timeout
- under seeded chaos the app failed in 4/4 runs where this function's dependency calls were faulted

```java
    static Rate fetchRate(String base, Parcel p) throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + "/v1/rates?zip=" + p.zip() + "&grams=" + p.grams())).build();
        HttpResponse<String> res = HTTP.send(req, HttpResponse.BodyHandlers.ofString());
        if (res.statusCode() != 200) throw new IOException("rates answered " + res.statusCode());
        Map<String, Object> m = Json.object(res.body());
        return new Rate((String) m.get("service"), ((Long) m.get("cents")).intValue());
    }
```

How callers use the result (a fallback must be something they already handle):

```java
// src/shipping/Main.java:17
            int total = 0;
            for (Parcel p : PARCELS) {
                Rate rate = Carrier.fetchRate(carrier.url(), p);
                Booking booking = Carrier.bookShipment(carrier.url(), p, rate);
                String code = Labels.labelCode(booking.tracking());
                total += rate.cents();
                System.out.printf("%s %-7s %5d c  %s  label %s%n", p.order(), rate.service(), rate.cents(), booking.tracking(), code);
```

Recorded examples:
- `fetchRate('http://127.0.0.1:46397', {'order': 'ORD-5001', 'zip': '8001', 'grams': 1200})` → {'service': 'parcel', 'cents': 690}
  dependency I/O: GET http://127.0.0.1:46397/v1/rates?zip=8001&grams=1200 -> 200
- `fetchRate('http://127.0.0.1:46397', {'order': 'ORD-5002', 'zip': '1010', 'grams': 350})` → {'service': 'letter', 'cents': 477}
  dependency I/O: GET http://127.0.0.1:46397/v1/rates?zip=1010&grams=350 -> 200
- `fetchRate('http://127.0.0.1:46397', {'order': 'ORD-5003', 'zip': '75011', 'grams': 4800})` → {'service': 'freight', 'cents': 1590}
  dependency I/O: GET http://127.0.0.1:46397/v1/rates?zip=75011&grams=4800 -> 200

## The contract (what `verify` enforces)

1. **Same happy path.** With healthy dependencies, return exactly what the original returned for
   every recorded call (compared with structural equality of the results as JSON). Make the same requests (same method, URL, body); you
   may add headers (e.g. `Idempotency-Key`) and must pass a timeout. Where the original raised,
   raise the same exception type (or a subclass).
2. **Bounded latency: 5 s per call**, everything included: timeouts, backoff sleeps,
   Retry-After waits. Every boundary call needs its own timeout (`HttpRequest.Builder.timeout`, `setConnectTimeout` + `setReadTimeout`, `Process.waitFor(n, unit)` then `destroyForcibly`); a call with no timeout that
   hangs is a hard fail. Budget the attempts so worst case stays under 5 s.
3. **Retry only transient failures**: timeouts, connection errors, HTTP 408/425/429/500/502/503/504
   (honour `Retry-After`), `HttpTimeoutException`, `SocketTimeoutException`, `ConnectException`, a 2xx body that doesn't parse (cut off), a process that times out, exits 75 or can't be forked (`error=11`). Exponential
   backoff with full jitter. **Never retry other 4xx** or programming errors. Retries must not
   multiply an outage: use a `Policy` (below), whose retry budget and circuit breaker cap them;
   verify reports the attempts per call under faults and with the dependency down.
4. **Idempotency.** Never send a POST/PUT/PATCH/DELETE twice unless every attempt carries the same
   `Idempotency-Key` header, derived deterministically from the logical operation (e.g. the invoice
   id), not random per attempt. A timed-out POST may already have taken effect.
5. **Degrade explicitly, never silently.** When the dependency stays down, either raise a structured
   error (`throw new RasRt.RasError(message, cause, "dependency")`, or `RasRt.RasIOException` where callers catch IOException) or return the declared fallback.
   Declare `static final <type> RAS_FALLBACK = <value>;` (or a static method `RAS_FALLBACK` with the same parameters) in class `Hardened`.
   Any other value that differs from the happy result counts as **silent wrong data** and fails.
   Swallowing errors (`catch (Exception e) { return null; }`) is the defect you are fixing.
   A fallback counts only in a call where a dependency request really failed and never got a good
   answer (the recorder watches the boundary; a log line alone proves nothing). The one exception is an open
   circuit: no request is needed if this dependency was seen to fail, and not to answer since, in the
   last 60 s (verify also runs your function through a long outage and recovery, state kept).
6. **Serviceable errors.** Log each give-up (`Policy` does; otherwise `RasRt.logEvent("ras.dependency.gave_up", RasRt.map("ras.dependency", name, "ras.fault", RasRt.faultClass(e)))`, which carries the `trace_id`) and keep the cause. Log before returning RAS_FALLBACK.

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

## Helpers (`RasRt`, standard library only)

Keep one `final class Hardened` in the file, with `static fetchRate`; helper methods, constants and types go inside it
(they may use everything the original class can). No reflection, no raw sockets.

```java
private static final RasRt.Policy RATES = RasRt.policy("rates").deadline(Duration.ofMillis(4500)).attemptTimeout(Duration.ofMillis(3300)).attempts(4);

// a read: check() turns 4xx/5xx into HttpStatusException, parseJson() a cut-off body into BadBodyException; both are classified
Object body = RATES.call(a -> RasRt.parseJson(RasRt.check(HTTP.send(a.apply(HttpRequest.newBuilder(uri)).build(), BodyHandlers.ofString())).body()));
// a write: the key makes it repeatable; a.apply() sends a.headers() (Idempotency-Key) and sets a.timeout()
ORDERS.call("POST", RasRt.idempotencyKey("order", id), a -> ...);
// a process: killed at the timeout; exit 75 or killed = ProcessFailedException; check other codes and the output yourself
RasRt.Result r = LABELS.call(a -> RasRt.run(a, "label-tool", id));
```

- `Policy.call` retries transient errors only (inside the deadline, honouring Retry-After) and throws `RasError` (cause kept, logged) when it gives
  up; what isn't transient is rethrown as it is. A refused attempt costs no time, so four attempts ride out a short restart inside the same deadline;
  the retry budget and the breaker keep that from multiplying an outage. `alsoRetry(predicate)` adds your parser's own exceptions for cut-off bodies.
- **A parser that ignores the rest of a cut-off body returns wrong data**: check that the body is complete inside the call, and output of processes too.
- `RasRt.CircuitBreaker` (`allow/success/failure`), `isTransient(e)`, `faultClass(e)`, `logEvent(event, map(...))`, `RasError`/`RasIOException` (`problem()`,
  RFC 9457), `HttpStatusException.status`, `requireApproval(op, details)`, `health()`.

Patterns in `RasRt.Policy`, on by default:
- `fallback: callCached(key, maxAge, fn)` (`stale_ttl_s=60.0`, `max_entries=1000`): a read given a cache key is answered from the last good answer while that is young enough; set `degraded` for a declared value. Only where callers can live with that answer.
- `deadline: deadline(Duration)` (`timeout_s=4.8`): the whole call, waits included. Passed on to nested policies and timeouts. Set it just under the latency budget.
- `idempotency: call("POST", key, fn)`: a write is repeated only if the call has an idempotency key; every attempt sends it (send the attempt's headers). 409 is asked again, 422 never.
- `retry_budget` (`ratio=0.1`, `min_per_s=2.0`, `burst=10.0`): retries are capped at a share of the calls plus a trickle, so an outage isn't multiplied. Nothing to do.
- `retry_after`: waits as long as a 429/503 asks, or gives up at once if that is past the deadline. Nothing to do.
- `retry: attempts(n), backoff(base, cap)` (`attempts=3`, `base_s=0.1`, `cap_s=2.0`): transient errors only, exponential backoff, full jitter.
- `circuit_breaker: breaker(threshold, cooldown)` (`fail_threshold=5`, `cooldown_s=1.0`, `failure_rate=0.5`, `window=50`, `min_calls=20`, `half_open_probes=1`): fails fast (a RejectedError) while the dependency is down, probes once per cooldown. A 429 doesn't open it. Nothing to do.
- `attempt_timeout: attemptTimeout(Duration)` (`timeout_s=3.5`): one attempt. Leave room for a second one inside the deadline.

Write it like production code: small, readable, commented where it matters. Then run
`ras-anything verify shipping.Carrier.fetchRate` and fix until it passes.
