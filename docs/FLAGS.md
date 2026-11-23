# Flags, canary, shadow, isolation

`.ras/flags.json` is an [OpenFeature flagd](https://flagd.dev/reference/flagd-json-evaluator/)-compatible file
the runtimes (Python `ras_anything.flags`, Node `patterns/flags.cjs`) poll for on every call. A broken file
keeps the last good one. `RAS_ANYTHING_DISABLE=1` is the master switch and is not stored here.

```sh
ras-anything flag list
ras-anything flag set ras.pattern.retry off          # every Policy, next call
ras-anything flag set ras.pattern.retry@shop-api on  # one dependency
ras-anything flag set weather.fetch_forecast --canary 5
ras-anything enable weather.fetch_forecast --canary 5,25,50,100
ras-anything enable weather.fetch_forecast --shadow
```

Keys:

| key | value | default if unset |
|---|---|---|
| `ras.pattern.<name>` | bool | the Policy's own configuration |
| `ras.pattern.<name>@<dependency>` | bool | the pattern-wide flag, then the Policy |
| `ras.enable.<id>` | 0–100 (percent of keys) | 100 (already enabled ⇒ hardened) |
| `ras.shadow.<id>` | bool | false |
| `ras.isolate.<dependency>` | `dependency` / `key` / `function` | `dependency` |

Canary routing is sticky by SHA-256 of the targeting key (first string argument, or `id` / `order_id` /
`idempotency_key`). Python and Node use the same hash, checked in `tests/test_flags.py`. Auto-promote and
auto-rollback are `control.analyse(window)`: availability must not drop more than 2 points, no new
duplicate writes, error rate must not double above 1%, p99 must not grow 20% without an availability gain;
enough canary calls, then the next step of the plan (default 5,25,50,100).

Shadow mode always serves the original. The hardened path is only run when a suppressor is registered
(so writes are not issued twice). Without one, the runtime logs `ras.shadow.skip` and does not double-call.

A bug in flag evaluation or the canary router falls back to the original function (fail-safe), tested by
fault-injecting the router.

Isolation: `Policy.shard(key)` gives a key its own breaker, budget and cache. A poison key cannot open
the breaker the others use. Measured in `tests/test_flags.py`.

Hot update (no restart, 0 dropped calls) is measured by `examples/flags-hot/measure.py`.
