"""Hardened billing.charge, written for ras-anything (see TASK.md). Swapped in by `ras-anything enable`."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from ras_anything.runtime import RasError, is_transient, log_event, retry

# The caller (app.py) already treats None as "payment failed, retry tomorrow", so that is the
# explicit degraded result when the provider stays down.
RAS_FALLBACK = lambda *args, **kwargs: None  # noqa: E731

DEADLINE_S = 4.5         # whole call, including timeouts and backoff (contract: 5 s)
ATTEMPT_TIMEOUT_S = 3.5  # long enough for a slow-but-healthy provider to answer
MIN_ATTEMPT_S = 0.2      # don't start an attempt with less time than this left
ATTEMPTS = 3


def _retryable(exc):
    # A 2xx whose body is cut off or not JSON is worth another try (same Idempotency-Key).
    return is_transient(exc) or isinstance(exc, ValueError)


def charge(invoice):
    """Charge one invoice. Returns the provider's charge record, or None if it didn't go through."""
    api = os.environ.get("PAYMENTS_API_URL", "https://api.payments.example")
    payload = json.dumps({"invoice": invoice["id"], "amount_cents": invoice["amount_cents"],
                          "currency": invoice["currency"]}).encode()
    # A timed-out POST may already have charged the card: every attempt carries the same key,
    # derived from the invoice, so the provider can deduplicate retries.
    headers = {"Content-Type": "application/json", "Idempotency-Key": f"charge-{invoice['id']}"}
    deadline = time.monotonic() + DEADLINE_S

    def attempt():
        remaining = deadline - time.monotonic()
        if remaining < MIN_ATTEMPT_S:
            raise TimeoutError("billing.charge: deadline reached before next attempt")
        req = urllib.request.Request(f"{api}/v1/charges", data=payload, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=min(ATTEMPT_TIMEOUT_S, remaining)) as resp:
            return json.loads(resp.read())

    try:
        return retry(attempt, attempts=ATTEMPTS, base=0.2, cap=1.0, budget_s=DEADLINE_S - MIN_ATTEMPT_S,
                     retry_on=_retryable)
    except (urllib.error.URLError, OSError, ValueError) as e:
        # Includes HTTPError (4xx/5xx), timeouts, refused connections and bad bodies.
        prob = RasError("billing charge failed", cause=e, dependency="charges").problem
        log_event("billing.charge.gave_up", invoice=invoice["id"], amount_cents=invoice["amount_cents"],
                  error=type(e).__name__, problem=prob)
        return RAS_FALLBACK(invoice)
