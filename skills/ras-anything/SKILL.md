---
name: ras-anything
description: Make a Python or Node.js (JavaScript/TypeScript) app survive dependency failures (timeouts, refused connections, 5xx/429, slow or truncated responses, outages) by hardening only its weakest functions and proving it on recorded real calls. Use when the user wants their app more reliable/available, asks about timeouts, retries, idempotency, circuit breakers, chaos testing, or "why does it hang/crash when the API is down".
---

# ras-anything

The `ras-anything` CLI measures, records, replays, checks and swaps. You write the hardened function.
(The same steps are MCP tools: `ras_scan`, `ras_harden`, `ras_verify`, `ras_enable`, `ras_bench`, `ras_report`, `ras_status`.)

## Loop

1. **Scan**: `ras-anything scan -- python app.py` or `ras-anything scan -- node app.mjs`, with the command the user runs their app with (a
   representative workload that succeeds when nothing fails). It runs once clean and 5 times under seeded
   chaos. Pick the top function marked "harden it".
2. **Harden**: `ras-anything harden <id>`. It records up to 500 real calls with the dependency I/O each
   made, and creates `.ras/hardens/<id>/` with `hardened.py` or `hardened.mjs` (the original, to edit) and `TASK.md`.
   Read TASK.md fully: it has the contract, the fault matrix, the callers and recorded examples.
3. **Write the hardened function** in that stub file:
   - Same happy path: same requests, same result for every recorded call. Where the original raised, same type.
   - `timeout=` on every dependency call; total worst case under the budget (default 5 s), sleeps included.
   - Retry only transient errors (timeouts, connection errors, 408/425/429/5xx, truncated 2xx bodies), with
     full-jitter backoff (`ras_anything.runtime.retry`, or `await aretry(...)` in async code). Never retry other 4xx.
   - POST/PUT/PATCH/DELETE: one deterministic `Idempotency-Key` across all attempts, or don't retry.
   - When the dependency stays down: return the declared `RAS_FALLBACK` (only a value callers already handle)
     or raise `RasError(..., cause=e, context={...}) from e`. Log with `log_event`. Never swallow. A fallback only
     counts in a call where a request was seen to fail and never got a good answer.
4. **Verify**: `ras-anything verify <id>` replays every recorded call (no network, virtual clock), then the
   fault matrix for the original and your version. Fix and re-run until PASS. Never edit `calls.json`/`meta.json`.
5. **Enable**: `ras-anything enable <id>`, then `ras-anything bench` and `ras-anything report`.
6. Tell the user the availability before → after (verify and bench), duplicate side effects and p99, and how
   to undo (`ras-anything disable <id>`, or `RAS_ANYTHING_DISABLE=1` for one run).

## Don't

- Don't edit the original function: the block appended to its module does the swap.
- Don't enable a hardened function that hasn't passed verify (enable refuses anyway).
- Don't "pass" by catching everything and returning a default: undeclared values count as silent wrong data,
  and `ChaosHang` (a BaseException) can't be caught by `except Exception`.
- If a function shows ✗ (nested, generator, property, uses super(), raw socket, arguments that can't be stored as JSON), say why it can't be hardened
  yet. Suggest moving the dependency call into a top-level function or method when that's the blocker.
