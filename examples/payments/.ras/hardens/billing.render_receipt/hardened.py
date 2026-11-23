"""Hardened billing.render_receipt, written for ras-anything (see TASK.md). Swapped in by `ras-anything enable`."""

from __future__ import annotations

import json
import subprocess
import sys

from ras_anything.runtime import RasError, is_transient, log_event, retry
from billing import RENDER  # module-level names the original uses

# Rendering is a pure, local side-effect-free step, so retries are safe. Worst case:
# 4 attempts x 1.0 s timeout + backoff (0.1 + 0.2 + 0.3) = 4.6 s, under the 5 s budget.
ATTEMPTS = 4
ATTEMPT_TIMEOUT_S = 1.0
BACKOFF_BASE_S = 0.1
BACKOFF_CAP_S = 0.3
EX_TEMPFAIL = 75


def _unrendered(invoice, charge_record):
    # The charge already went through; a missing receipt must not hide that. The caller prints
    # receipt['bytes'] and receipt['sha256'][:8], so keep that shape but make it obviously unrendered.
    return {"pages": 0, "bytes": 0, "sha256": "unrendered"}


# Declared degraded result when the renderer stays down (a re-render can be done later).
RAS_FALLBACK = _unrendered


class RenderTempFail(Exception):
    """The renderer exited with EX_TEMPFAIL (75): try again."""


def _is_retryable(exc):
    # Timeouts, temp-fail exits, and truncated/undecodable stdout from a successful exit.
    return isinstance(exc, (RenderTempFail, json.JSONDecodeError)) or is_transient(exc)


def render_receipt(invoice, charge_record):
    """Render the receipt in a separate process; returns its page count, size and hash."""
    receipt = {"invoice": invoice["id"], "customer": invoice["customer"], "amount_cents": invoice["amount_cents"],
               "currency": invoice["currency"], "charge_id": charge_record["id"]}
    argv = [sys.executable, RENDER, json.dumps(receipt)]

    def attempt():
        out = subprocess.run(argv, capture_output=True, text=True, timeout=ATTEMPT_TIMEOUT_S)
        if out.returncode == EX_TEMPFAIL:
            raise RenderTempFail(f"renderer exited {EX_TEMPFAIL}: {out.stderr.strip()[:200]}")
        return json.loads(out.stdout)

    try:
        return retry(attempt, attempts=ATTEMPTS, base=BACKOFF_BASE_S, cap=BACKOFF_CAP_S,
                     budget_s=4.0, retry_on=_is_retryable)
    except Exception as e:
        if not _is_retryable(e):
            raise  # programming errors and permanent failures surface unchanged
        prob = RasError("receipt render failed", cause=e, dependency="render.py").problem
        context = {"invoice": invoice["id"], "charge_id": charge_record["id"],
                   "error": type(e).__name__, "problem": prob}
        log_event("render_receipt.degraded", **context)
        return RAS_FALLBACK(invoice, charge_record)
