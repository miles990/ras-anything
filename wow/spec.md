# RAS-anything v0.1 — spec (wow 輕量交付 + 七關)

Owner: Alex Lee (miles990). Request (2026-11-23, pt507u): 取 fast-anything 設計核心，fast → Reliability, Availability, Serviceability；用 Claude Fable 當 wow 評審；新建 repo 做 RAS-anything。

## First-principles check
- **facts**: fast-anything's value = (1) measure, don't guess; (2) touch only the weakest few units; (3) a coding agent writes the change; (4) *proof* on real recorded behaviour before swap; (5) reversible swap (marked block + kill switch). None of that is specific to speed. RAS failures in small Python services come overwhelmingly from the *boundary* (network/subprocess/IO): no timeout → hang, no retry → transient error becomes outage, retry without idempotency → duplicate side effects, swallowed exceptions → silent wrong data, no context in errors → slow diagnosis.
- **constraints**: no paid tools; private repo; no publish/post; CI budget tight; Fable judge-only; no fabricated numbers; MIT credit to fast-anything; builder ≠ judge.
- **goal**: one command that shows, with measured numbers on the user's own workload, how much more available a Python app becomes under realistic dependency faults after hardening only its weakest function(s) — and proves the happy path did not change.
- **rebuild (smallest mechanism)**: Python-only, stdlib-only tool (no Rust: reliability is control flow, not CPU; the target apps are Python; zero-dep install). Fault injection at the boundary (monkeypatched `urllib.request.urlopen`, `subprocess.run`, optional `requests`), deterministic seeded fault plans, outcome classification per call. Node deferred (prove the loop in one language first).
- **decision-seat**: internal gates = AI (Fable 5-1 judge, Codex second opinion); anything external (making repo public, publishing, posting) = Alex.
- **verdict**: proceed (reshape fast→RAS as above).

## External spec items (S-n)
- S-1 `ras-anything scan -- <cmd>`: runs the user's workload once clean and once under seeded chaos; static+dynamic signals per top-level function (boundary calls without timeout, no retry/backoff, bare/swallowing except, non-idempotent writes retried, missing error context/trace id, unclosed resources); ranks by **risk × exposure × hardenability**; prints a ranked table with ids and a one-line "why" + verdict (harden it / can't yet: reason).
- S-2 `ras-anything harden [id] [--agent claude|codex|<cmd>]`: re-runs workload, records up to 500 real calls of the unit (args, return, and the boundary I/O it performed — VCR style), creates `.ras/hardens/<id>/` with `hardened.py` stub + `TASK.md` (contract: same happy-path results; bounded latency; declared fallback; idempotency rules; structured errors). With `--agent`, pipes TASK.md to the agent.
- S-3 `ras-anything verify [id]`: (a) happy-path replay: every recorded call, boundary served from recordings, hardened result == original result (exceptions: same type where original raised); (b) chaos replay over a fixed fault matrix (timeout/hang, connection refused, HTTP 5xx, slow dependency, truncated body, sustained outage then recovery), original vs hardened; (c) safety checks: no new crash types, p99 latency ≤ budget, no added duplicate non-idempotent side effects (POST/PUT without Idempotency-Key), fallback values only those declared. Enable is refused unless all pass AND availability strictly improves.
- S-4 `ras-anything enable|disable [id]`: appends / removes one marked block to the function's module (byte-for-byte removal, checked by hash). `RAS_ANYTHING_DISABLE=1` forces originals at runtime.
- S-5 `ras-anything bench -- <cmd>`: whole-app runs under seeded chaos, interleaved original vs hardened, N seeds; reports availability % (correct-or-declared-degraded within budget), error/crash rate, p50/p99 latency, duplicate side effects, recovery calls after outage.
- S-6 `ras-anything report`: `RAS-REPORT.md` + `ras-card.svg` (before/after, honest labels: fault model, seeds, machine).
- S-7 `ras-anything doctor`: python version, agents (claude/codex) on PATH, git cleanliness, write access.
- S-8 `ras-anything go --agent <a> -- <cmd>`: scan → harden top → verify → enable → bench → report in one command.
- S-9 Agent surfaces: stdio MCP server (`ras-anything mcp`, tools ras_scan/harden/verify/enable/disable/bench/report/status), Claude Code plugin (`.claude-plugin/`, `commands/ras.md`), `skills/ras-anything/SKILL.md`.
- S-10 Runtime helpers `ras_anything.runtime` (stdlib): `timeout`-aware call, retry with full jitter + budget, circuit breaker, fallback, structured error with trace id + JSON log line. Hardened code may use them.
- S-11 Two realistic example apps in `examples/` with reproducible before/after numbers produced by actually running the pipeline (hardened code written by a real coding agent run, kept in repo).
- S-12 README: pitch, install (pip install -e / pipx from git), use table, proof section, measured results table (only measured), fault model, limits, credit to fast-anything (MIT).

## Domain points (W6)
Timeouts must be on every blocking boundary call; retries only on transient errors (timeouts, connection errors, 502/503/504, 429 honoring Retry-After), never on 4xx; exponential backoff with full jitter and a total time budget; never retry non-idempotent writes without an idempotency key; circuit breaker opens after consecutive failures and half-opens after cooldown; fallbacks must be explicit and distinguishable (degraded), never silent wrong data; errors carry cause + context + trace id; swallowing exceptions is a defect. Availability = good / valid requests within a latency SLO (Google SRE definition). Chaos must be deterministic (seeded) to be comparable.

## Wow moment (gap type ①②③)
"咦？這樣就好了？" — one command on your own app prints *availability under faults 61% → 99%* with the hardened diff being ~30 lines in one function, and verify shows the exact recorded call that the naive retry would have double-charged. Not arrived = numbers only on a toy that the tool was tuned for; or hardened code that "passes" by swallowing everything.

## Main flow & first-use target
`pip install -e .` → `ras-anything go --agent claude -- python examples/weather/app.py`. First-use (without README) target: understand ranked scan output and what to do next ≤ 30 s of reading (LLM proxy, marked 未驗效度).
