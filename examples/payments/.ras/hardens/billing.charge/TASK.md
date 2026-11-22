# Harden `billing.charge`

You are hardening one Python function for **reliability, availability and serviceability**.
Edit **only `hardened.py` in this folder**. Keep the function name `charge` and its signature.
When you are done, `ras-anything verify billing.charge` must print PASS. Run it yourself (it works from this
folder) and iterate until it passes. Do not enable anything; do not edit the original source,
`calls.pkl` or `meta.json`.

## The function

`billing.charge` in `billing.py` line 13. Recorded **16 real calls** from
`python app.py`. Dependency requests it made:
- 16× `POST http://127.0.0.1:33455/v1/charges`

What the scan found:
- retries a POST with no Idempotency-Key
- `except Exception: time.sleep(0.5)` swallows errors
- no logging on the failure path
- under seeded chaos the app failed in 1/1 runs where this function's dependency calls were faulted

```python
def charge(invoice):
    """Charge one invoice. Returns the provider's charge record, or None if it didn't go through."""
    api = os.environ.get("PAYMENTS_API_URL", "https://api.payments.example")
    payload = json.dumps({"invoice": invoice["id"], "amount_cents": invoice["amount_cents"],
                          "currency": invoice["currency"]}).encode()
    for attempt in range(3):  # the provider is flaky, so try a few times
        try:
            req = urllib.request.Request(f"{api}/v1/charges", data=payload, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception:
            time.sleep(0.5)
    return None
```

How callers use the result (a fallback must be something they already handle):

```python
# app.py:27
    print("BILLING RUN  %d invoices" % len(batch))
    for inv in batch:
        record = billing.charge(inv)
        if record is None:
            print(f"  {inv['id']}  {inv['amount_cents'] / 100:>8.2f}  PENDING (payment failed, retry tomorrow)")
            continue
        receipt = billing.render_receipt(inv, record)
```

Recorded examples:
- `charge({'id': 'INV-1001', 'customer': 'Acme Co', 'amount_cents': 1500, 'currency': 'us…)` → {'id': 'ch_0001', 'invoice': 'INV-1001', 'amount_cents': 1500, 'currency': 'usd', 'status': 'succeeded'}
  dependency I/O: POST http://127.0.0.1:33455/v1/charges -> 200
- `charge({'id': 'INV-1002', 'customer': 'Globex', 'amount_cents': 2237, 'currency': 'usd…)` → {'id': 'ch_0002', 'invoice': 'INV-1002', 'amount_cents': 2237, 'currency': 'usd', 'status': 'succeeded'}
  dependency I/O: POST http://127.0.0.1:33455/v1/charges -> 200
- `charge({'id': 'INV-1003', 'customer': 'Initech', 'amount_cents': 2974, 'currency': 'us…)` → {'id': 'ch_0003', 'invoice': 'INV-1003', 'amount_cents': 2974, 'currency': 'usd', 'status': 'succeeded'}
  dependency I/O: POST http://127.0.0.1:33455/v1/charges -> 200

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
   Declare it at module level as `RAS_FALLBACK` (a value, or a callable taking the same arguments;
   to declare `None` as the fallback use `RAS_FALLBACK = lambda *args, **kwargs: None`).
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
`ras-anything verify billing.charge` and fix until it passes.
