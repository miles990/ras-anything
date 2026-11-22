# Fresh-User First-Read Test Summary (W3)

Two isolated blind runs were conducted outside the repo via `wow-agent --blind` (raw logs in gitignored `wow/work/blind/`):
1. **Run 1 (`blind1`, `workspace-write` sandbox)**: Blocked at `scan` because Codex's `workspace-write` sandbox denies `socket()` (`PermissionError: [Errno 1] Operation not permitted`). Tool cleanly refused to scan/harden on failed clean run (`0` wrong guesses from unclear output).
2. **Run 2 (`blind2`, `danger-full-access` sandbox, Phase A no-README + Phase B README)**:
   - **Phase A (CLI only, no README)**: 12 steps, **0 wrong guesses due to unclear output**. Used `scan` → `harden` → edited `hardened.py` → `verify` PASS → `enable` → `bench` (caught 1/20 wrong output on seed 2 because `holdout_app` mock server doesn't deduplicate `Idempotency-Key`) → `report` → `disable` (byte-for-byte restore).
   - **Phase B (README)**: Noted four concrete gaps:
     1. In the blind bundle (`pkg/` subdir), `pip install -e .` had to run from `pkg/` (expected in blind harness).
     2. README didn't mention the manual workflow (omit `--agent`) or that `go` leaves the swap enabled after `bench` — **fixed in `README.md` and `go.py` warning**.
     3. `bench` reported `wrong` without printing the differing stdout/faults — **fixed in `bench.py` (`first_wrong` printed and saved)**.
     4. `doctor` reports `claude`/`codex` on PATH without testing whether they can actually execute (noted as honest gap; PATH check only).
