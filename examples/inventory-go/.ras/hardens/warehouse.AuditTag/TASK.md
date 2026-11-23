# Harden `warehouse.AuditTag`

You are hardening one Go function (package-level, compiled) for **reliability, availability and serviceability**.
Edit **only `hardened.go` in this folder**. Keep the function name `AuditTag` and its signature.
When you are done, `ras-anything verify warehouse.AuditTag` must print PASS. Run it yourself (it works from this
folder) and iterate until it passes. Do not enable anything; do not edit the original source,
`calls.json` or `meta.json`.

## The function

`warehouse.AuditTag` in `warehouse/warehouse.go` line 61. Recorded **6 real calls** from
`go run .`. Dependency requests it made:
- 1× `RUN sh -c printf '{"tag":"BIN-sku-101"}'`
- 1× `RUN sh -c printf '{"tag":"BIN-sku-102"}'`
- 1× `RUN sh -c printf '{"tag":"BIN-sku-103"}'`
- 1× `RUN sh -c printf '{"tag":"BIN-sku-104"}'`
- 1× `RUN sh -c printf '{"tag":"BIN-sku-105"}'`
- 1× `RUN sh -c printf '{"tag":"BIN-sku-106"}'`

What the scan found:
- outbound call without timeout
- no retry on transient errors
- under seeded chaos the app failed in 6/6 runs where this function's dependency calls were faulted

```go
func AuditTag(sku string) (string, error) {
	out, err := exec.Command("sh", "-c", fmt.Sprintf(`printf '{"tag":"BIN-%s"}'`, sku)).Output()
	if err != nil {
		return "", err
	}
	var parsed struct {
		Tag string `json:"tag"`
	}
	if err := json.Unmarshal(out, &parsed); err != nil {
		return "", err
	}
	return parsed.Tag, nil
}
```

How callers use the result (a fallback must be something they already handle):

```go
// main.go:92
			os.Exit(1)
		}
		tag, err := warehouse.AuditTag(sku)
		if err != nil {
			fmt.Fprintf(os.Stderr, "audit %s failed: %v\n", sku, err)
			os.Exit(1)
		}
```

Recorded examples:
- `AuditTag('sku-101')` → 'BIN-sku-101'
  dependency I/O: RUN sh -c printf '{"tag":"BIN-sku-101"}' -> 0
- `AuditTag('sku-102')` → 'BIN-sku-102'
  dependency I/O: RUN sh -c printf '{"tag":"BIN-sku-102"}' -> 0
- `AuditTag('sku-103')` → 'BIN-sku-103'
  dependency I/O: RUN sh -c printf '{"tag":"BIN-sku-103"}' -> 0

## The contract (what `verify` enforces)

1. **Same happy path.** With healthy dependencies, return exactly what the original returned for
   every recorded call (compared with `reflect.DeepEqual` / JSON structural equality). Make the same requests (same method, URL, body); you
   may add headers (e.g. `Idempotency-Key`) and must pass `context.Context` with `context.WithTimeout`. Where the original raised,
   raise the same exception type (or a subclass).
2. **Bounded latency: 5 s per call**, everything included: timeouts, backoff sleeps,
   Retry-After waits. Every boundary call needs a timeout (`context.WithTimeout` + `http.NewRequestWithContext`, `exec.CommandContext`); a call with no timeout that
   hangs is a hard fail. Budget the attempts so worst case stays under 5 s.
3. **Retry only transient failures**: timeouts, connection errors, HTTP 408/425/429/500/502/503/504
   (honour `Retry-After`), `net.Error` timeouts, `ECONNREFUSED`, HTTP 429/5xx, truncated JSON bodies, or subprocess exit 75. Exponential
   backoff with full jitter. **Never retry other 4xx** or programming errors. Retries must not
   multiply an outage: use a `Policy` (below), whose retry budget and circuit breaker cap them;
   verify reports the attempts per call under faults and with the dependency down.
4. **Idempotency.** Never send a POST/PUT/PATCH/DELETE twice unless every attempt carries the same
   `Idempotency-Key` header, derived deterministically from the logical operation (e.g. the invoice
   id), not random per attempt. A timed-out POST may already have taken effect.
5. **Degrade explicitly, never silently.** When the dependency stays down, either raise a structured
   error (`&ras_hardened.RasError{Message: "...", Cause: err, Dependency: "..."}`) or return the declared fallback.
   Declare `var RAS_FALLBACK = <value>` at package level in `hardened.go` to allow degraded fallback returns.
   Any other value that differs from the happy result counts as **silent wrong data** and fails.
   Swallowing errors (`if err != nil { return zero, nil }` or `_ = client.Do(req)`) is the defect you are fixing.
   A fallback counts only in a call where a dependency request really failed and never got a good
   answer (the recorder watches the boundary; a log line alone proves nothing). The one exception is an open
   circuit: no request is needed if this dependency was seen to fail, and not to answer since, in the
   last 60 s (verify also runs your function through a long outage and recovery, state kept).
6. **Serviceable errors.** Log each give-up with `_ras_rt.LogEvent("ras.dependency.gave_up", attrs)` (includes `trace_id`) and wrap causes with `&ras_hardened.RasError{...}`.

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

## Helpers (`_ras_rt` = `package ras_hardened`, Go stdlib only)

Keep the package declaration (`package ...`) as in the stub (`hardened.go` compiles inside the target package).
- `_ras_rt.NewCircuitBreaker(name, threshold, cooldown)`: open circuit after consecutive failures; call `cb.Allow()`, `cb.Success()`, `cb.Failure()`.
- `_ras_rt.Retry(ctx, opts, fn)`: full-jitter exponential backoff honouring `Retry-After` and shared `ctx` deadline.
- `_ras_rt.IsTransient(err)`: transient failure classifier (network timeouts, refused, 429, 502/503/504).
- `_ras_rt.IdempotencyKey(parts...)`: deterministic key for `Idempotency-Key` header on `POST`/`PUT`/`PATCH`/`DELETE`.
- `_ras_rt.RasError`: structured error with `Problem()` (RFC 9457) and `trace_id`.
- `_ras_rt.LogEvent(event, attrs)`: structured OTel JSON line on stderr with `trace_id` (scrubbed).

Patterns configured by default:
- `fallback` (`stale_ttl_s=60.0`, `max_entries=1000`): a read given a cache key is answered from the last good answer while that is young enough; set `degraded` for a declared value. Only where callers can live with that answer.
- `deadline` (`timeout_s=4.8`): the whole call, waits included. Passed on to nested policies and timeouts. Set it just under the latency budget.
- `idempotency`: a write is repeated only if the call has an idempotency key; every attempt sends it (send the attempt's headers). 409 is asked again, 422 never.
- `retry_budget` (`ratio=0.1`, `min_per_s=2.0`, `burst=10.0`): retries are capped at a share of the calls plus a trickle, so an outage isn't multiplied. Nothing to do.
- `retry_after`: waits as long as a 429/503 asks, or gives up at once if that is past the deadline. Nothing to do.
- `retry` (`attempts=3`, `base_s=0.1`, `cap_s=2.0`): transient errors only, exponential backoff, full jitter.
- `circuit_breaker` (`fail_threshold=5`, `cooldown_s=1.0`, `failure_rate=0.5`, `window=50`, `min_calls=20`, `half_open_probes=1`): fails fast (a RejectedError) while the dependency is down, probes once per cooldown. A 429 doesn't open it. Nothing to do.
- `attempt_timeout` (`timeout_s=3.5`): one attempt. Leave room for a second one inside the deadline.

Write it like production code: small, readable, commented where it matters. Then run
`ras-anything verify warehouse.AuditTag` and fix until it passes.
