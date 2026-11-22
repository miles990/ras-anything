# Reviews — RAS-anything v0.1

## Round 1 (commit `cce2c98`)

- **Claude Fable 5-1 (`fable1`)**: **未達成 (as a proof tool), 達成 (as a demo)**
  - Gates: W1 通過, W2 通過 with nits, W3 未驗證, W4 未通過 (lie-factor: asymmetric degraded accounting + omitted strict identical row), W5 未通過 (holdout `retry_post.py` passed with `dups: 0` because `FirstAttempt` faulted GET and POST simultaneously), W6 未通過 (verify holes), W7 通過 with reservations (absolute paths in committed `.ras/`).
- **Codex second opinion (`codex1`)**: **FIX-FIRST**
  - Raised 11 defects (P1: per-key isolation, self-declared `RAS_FALLBACK` abuse, skipped `original_nondeterministic`, missing new-crash gate, escaping absolute paths in `meta.json`/`enable.py`, pickle cassettes; P2: per-call outage, CRLF/stacked disable hash check, bench degraded strictness, bench runtime RNG seeding, MCP version negotiation).
- **Holdout Round 1 (`holdout-verify.txt`)**: `good_hardening.py` FAILED (`10.0% → 10.0%`), `retry_post.py` FALSELY PASSED (`10.0% → 40.0%`, `dups 0`).

## Fix pass (commit `dc2942a`)

One surgical pass applied (plus re-measurement of `examples/weather` and `examples/payments` with the same agent-written `hardened.py`):
- Per-key isolated fault scenarios for multi-request calls (`_verify_worker.py`, `faults.py`).
- Unlogged `RAS_FALLBACK` rejected (`wrong`), fail-closed `happy` on `original_nondeterministic > 0`, `no_new_crashes` gate (`_verify_worker.py`).
- Project-relative paths stored in `scan.json`, `meta.json`, `state.json`; `resolve_in_project` containment + symlink check + CRLF byte preservation + stacked disable hash tracking (`paths.py`, `scan.py`, `harden.py`, `enable.py`, `bench.py`).
- Subprocess `Req.key` normalized across venvs/checkouts (`boundary.py`); `runtime.seed` in bench (`_agent.py`).
- Symmetric degraded stdout accounting + `first_wrong` diff diagnostic (`bench.py`), `identical output` row on SVG cards (`report.py`), `go` warning on `wrong_rate > 0` (`go.py`).
- Single-probe half-open `CircuitBreaker` (`runtime.py`), narrow swallow signal (`static.py`), MCP protocol negotiation (`mcp.py`), updated README/REPRODUCE and 5 new unit tests (`33/33` passing).

## Round 2 re-judge (commit `dc2942a`)

- **Claude Fable 5-1 (`fable2`)**: **達成**
  - Gates: **W1 通過**, **W2 通過**, **W3 通過 on comprehension (≤30 s 未驗證)**, **W4 通過**, **W5 通過**, **W6 通過 with reservations**, **W7 通過 with reservations**.
- **Codex second opinion (`codex2`, `--sandbox read-only`)**: **FIX-FIRST** (residual hardening/security items)
  - Verified FIXED: #1 per-key GET-before-POST retry caught (`holdout-verify-v2.txt` rejects `retry_post.py` on `dups: 0→2` and accepts `good_hardening.py` under `--budget 1`), #10 deterministic runtime jitter seeding.
  - Remaining P1/P2 gaps (honestly reported, deferred beyond v0.1):
    - P1: self-declared `RAS_FALLBACK` with a `"trace_id"` log line can still mask a swallowed exception; `no_new_crashes` checks a fixed `BUG_EXC` set; `_runtime.py` copy destination / pre-resolution symlink check in `enable.py`; `pickle.load` on repo `calls.pkl` + `shell=True` custom agent command.
    - P2: sustained cross-call outage & recovery (S-5) unmeasured; `bench` `classify` credits any run with `fallbacks > 0` as degraded without latency budget or per-line fallback verification; `examples/` committed `cmd`/`executable`/`sys_path` still carry builder paths for re-record/bench; `README.md` cites a GET-then-POST test in the unit suite that lives only in the gitignored holdout and cites `~3 s` for `unittest` (measured `2.2s` on this host); `disable` mutates source before resolving other entries.
