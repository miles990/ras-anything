# Holdout Blind Test Summary (`inventory.sync_item`)

Independent holdout written by Codex before/outside the build (moved after testing into gitignored `wow/work/holdout/`):
`holdout_app/` (`GET /stock/bolt-42` + `POST /replenishments` + `optional_note`), `good_hardening.py` (1 s budget, retries GET only, sends POST once), and 3 deliberately defective variants in `bad_hardenings/` (`swallow.py`, `retry_post.py`, `change_output.py`).

## Round 1 (pre-fix, `cce2c98`) — FAILED blind acceptance

Raw log: `wow/work/evidence/holdout-verify.txt` (gitignored).
- `good_hardening.py` (default 5 s budget): **FAIL** (`availability` 10.0% → 10.0%). Because `FirstAttempt` faulted both `GET` and `POST` simultaneously, GET resilience never improved availability while POST was unkeyed.
- `bad_hardenings/retry_post.py`: **FALSE PASS** (`availability` 10.0% → 40.0%, `dups` 0 → 0). GET timeout burned the 1 s budget before POST ran, masking POST retry after commit.
- `bad_hardenings/swallow.py`: **FAIL** (`no_wrong`, `availability`).
- `bad_hardenings/change_output.py`: **FAIL** (`happy`, `no_wrong`, `availability`).

## Round 2 (after P0/P1 fix pass) — ALL CONTROLS CORRECT

Raw log: `wow/work/evidence/holdout-verify-v2.txt` (gitignored).
Run with `--budget 1` (matching `expected.md` and `good_hardening.py`'s 1 s deadline) and per-key fault isolation on multi-request calls:

| candidate | happy | availability (orig → hard) | wrong | hangs | p99 (1 s) | dups (orig → hard) | verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| `good_hardening.py` | 1/1 | 4.2% → **12.5%** | 0 | 0 | 1.00s | 0 → 0 | **✓ PASS** |
| `bad_hardenings/swallow.py` | 1/1 | 4.2% → 12.5% | 21 | 0 | 1.00s | 0 → 0 | **✗ FAIL** (`no_wrong`) |
| `swallow.py` + unlogged `RAS_FALLBACK` | 1/1 | 4.2% → 12.5% | 21 | 0 | 1.00s | 0 → 0 | **✗ FAIL** (`no_wrong`) |
| `bad_hardenings/retry_post.py` | 1/1 | 4.2% → 45.8% | 0 | 0 | 2.00s | 0 → **2** | **✗ FAIL** (`no_hang`, `p99`, `dups`) |
| `bad_hardenings/change_output.py` | 0/1 | 4.2% → 0.0% | 3 | 0 | 1.00s | 0 → 0 | **✗ FAIL** (`happy`, `no_wrong`, `availability`) |

`enable` / `RAS_ANYTHING_DISABLE=1` / `disable` on `holdout_app/inventory.py` also succeeded and restored the original file byte-for-byte. Static scan additionally flagged `inventory.optional_note` (`except OSError: return None` swallows errors).
