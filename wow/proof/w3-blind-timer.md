# W3 Blind Newbie 30-Second First-Result Proof

- **Date**: 2026-11-23 18:13 (Asia/Taipei, UTC+8)
- **Target**: Read ONLY `README.md` in an isolated temp directory (no repo source, no `.ras/`, no skills loaded) → first useful scan result (`≤ 30s` target).
- **Runner**: `claude -p --model haiku --permission-mode bypassPermissions --tools Read,Bash --output-format stream-json`
- **Isolated workspace**: temp dir with `README.md` + `examples/weather/{app.py,upstream.py,weather.py}` (clean, no `.ras/`), using a fresh `install.sh` venv shim in an isolated `$HOME` (`~/.claude/skills` excluded).

## Measured Timeline

- **Total wall clock (`subprocess.run`)**: **12.10 s** (`duration_ms=9309`, `duration_api_ms=6465`)
- **Wrong guesses / retries**: **0**
- **Exact tool calls**:
  1. `Read README.md`
  2. `Bash: ras-anything scan -- python app.py`
- **First useful result obtained**:
  - Clean run: `121 ms`, `12` dependency calls
  - Chaos runs: `5/5` failed (4 crashes, 1 hang)
  - Top function: `weather.fetch_forecast` (score `81.8`, verdict `✓ harden it`, flagged `urlopen()` without timeout, no retry, no failure-path logging)
  - Saved `.ras/scan.json`, suggested next step `ras-anything harden weather.fetch_forecast --agent claude`.
