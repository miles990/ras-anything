# Harden `weather.fetch_forecast`

You are hardening one Python function for **reliability, availability and serviceability**.
Edit **only `hardened.py` in this folder**. Keep the function name `fetch_forecast` and its signature.
When you are done, `ras-anything verify weather.fetch_forecast` must print PASS. Run it yourself (it works from this
folder) and iterate until it passes. Do not enable anything; do not edit the original source,
`calls.pkl` or `meta.json`.

## The function

`weather.fetch_forecast` in `weather.py` line 9. Recorded **12 real calls** from
`python app.py`. Dependency requests it made:
- 12× `GET http://127.0.0.1:44801/v1/forecast?…`

What the scan found:
- urlopen() without timeout
- no retry on transient errors
- no logging on the failure path
- under seeded chaos the app failed in 3/3 runs where this function's dependency calls were faulted

```python
def fetch_forecast(city):
    """Three-day forecast for one city, as the provider returns it."""
    base = os.environ.get("WEATHER_API_URL", "https://api.weather.example")
    resp = urlopen(f"{base}/v1/forecast?city={quote(city)}")
    return json.loads(resp.read())
```

How callers use the result (a fallback must be something they already handle):

```python
# app.py:19
    rows = []
    for city in CITIES:
        rows.append((city, weather.summarize(weather.fetch_forecast(city))))
    print("WEATHER DASHBOARD")
    for city, line in rows:
        print(f"  {city:<10} {line}")
    live = sum(1 for _, line in rows if line != "unavailable")
```

Recorded examples:
- `fetch_forecast('Taipei')` → {'city': 'Taipei', 'daily': [{'day': 'Mon', 'hi_c': 8, 'lo_c': -1, 'rain_mm': 3.3}, {'day': 'Tue', 'hi_c': 13, 'lo_c': 3, 'rain_mm': 1.9}, {'day': 'Wed', 'hi_c': 20, 'lo_c': 14, 'rain_mm': 4.8}], 'source': 'local-upstream'}
  dependency I/O: GET http://127.0.0.1:44801/v1/forecast?city=Taipei -> 200
- `fetch_forecast('Tokyo')` → {'city': 'Tokyo', 'daily': [{'day': 'Mon', 'hi_c': 19, 'lo_c': 14, 'rain_mm': 2.8}, {'day': 'Tue', 'hi_c': 28, 'lo_c': 22, 'rain_mm': 7.9}, {'day': 'Wed', 'hi_c': 8, 'lo_c': 2, 'rain_mm': 0.6}], 'source': 'local-upstream'}
  dependency I/O: GET http://127.0.0.1:44801/v1/forecast?city=Tokyo -> 200
- `fetch_forecast('Seoul')` → {'city': 'Seoul', 'daily': [{'day': 'Mon', 'hi_c': 8, 'lo_c': 2, 'rain_mm': 0.7}, {'day': 'Tue', 'hi_c': 15, 'lo_c': 7, 'rain_mm': 5.9}, {'day': 'Wed', 'hi_c': 10, 'lo_c': 6, 'rain_mm': 1.0}], 'source': 'local-upstream'}
  dependency I/O: GET http://127.0.0.1:44801/v1/forecast?city=Seoul -> 200

## The contract (what `verify` enforces)

1. **Same happy path.** With healthy dependencies, return exactly what the original returned for
   every recorded call (compared with `==`). Make the same requests (same method, URL, body); you
   may add headers (e.g. `Idempotency-Key`) and must pass `timeout=`. Where the original raised,
   raise the same exception type (or a subclass).
2. **Bounded latency: 5 s per call**, everything included: timeouts, backoff sleeps,
   Retry-After waits. Every boundary call needs a timeout (`urlopen(..., timeout=t)`,
   `subprocess.run(..., timeout=t)`, `requests.get(..., timeout=t)`); a call with no timeout that
   hangs is a hard fail. Budget the attempts so worst case stays under 5 s.
3. **Retry only transient failures**: timeouts, connection errors, HTTP 408/425/429/500/502/503/504
   (honour `Retry-After`), a truncated/undecodable body from a 2xx, a subprocess exit 75. Exponential
   backoff with full jitter. **Never retry other 4xx** or programming errors.
4. **Idempotency.** Never send a POST/PUT/PATCH/DELETE twice unless every attempt carries the same
   `Idempotency-Key` header, derived deterministically from the logical operation (e.g. the invoice
   id), not random per attempt. A timed-out POST may already have taken effect.
5. **Degrade explicitly, never silently.** When the dependency stays down, either raise a structured
   error (`raise RasError("...", cause=e, context={...}) from e`) or return the declared fallback.
   Declare it at module level as `RAS_FALLBACK` (a value, or a callable taking the same arguments).
   Any other value that differs from the happy result counts as **silent wrong data** and fails.
   Swallowing errors (`except Exception: return None`) is the defect you are fixing.
6. **Serviceable errors.** Log each give-up with `log_event("...", **context)` (JSON line on stderr,
   with a trace id) and chain causes (`raise ... from e`).

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

## Helpers (`from ras_anything.runtime import ...`, standard library only)

- `retry(fn, *, attempts=3, base=0.1, cap=2.0, budget_s=None, retry_on=is_transient)`: calls `fn()`;
  full-jitter backoff, honours Retry-After, stops before crossing `budget_s`, re-raises the last
  error unchanged. `retry_on` may be a predicate or a tuple of exception types.
- `is_transient(exc)`: the transient rule from point 3 (not truncated bodies: add `ValueError` /
  `json.JSONDecodeError` yourself when a 2xx body fails to parse).
- `call_with_timeout(fn, timeout_s, *args)`: for code that has no timeout parameter.
- `CircuitBreaker(fail_threshold, cooldown_s)`: `.call(fn, *args)`; raises `CircuitOpenError` while open.
- `RasError(message, *, cause=None, context=None)`: structured error with a trace id.
- `log_event(event, **fields)`: one JSON line on stderr.

Write it like production code: small, readable, commented where it matters. Then run
`ras-anything verify weather.fetch_forecast` and fix until it passes.
