"""Test fixture: examples/payments charge hardened with a Policy (positive control for tests)."""

import json
import os
import urllib.request

from ras_anything.runtime import Policy, RasError, RejectedError, idempotency_key, is_transient, log_event

RAS_FALLBACK = None
payments = Policy("payments-api", deadline={"timeout_s": 4.5}, attempt_timeout={"timeout_s": 3.4},
                  retry={"attempts": 4, "retry_on": lambda e: is_transient(e) or isinstance(e, ValueError)})


def charge(invoice):
    api = os.environ.get("PAYMENTS_API_URL", "https://api.payments.example")
    payload = json.dumps({"invoice": invoice["id"], "amount_cents": invoice["amount_cents"],
                          "currency": invoice["currency"]}).encode()

    def once(attempt):
        req = urllib.request.Request(f"{api}/v1/charges", data=payload, method="POST",
                                     headers={"Content-Type": "application/json", **attempt.headers})
        with urllib.request.urlopen(req, timeout=attempt.timeout_s) as resp:
            return json.loads(resp.read())

    try:
        return payments.call(once, method="POST", idempotency_key=idempotency_key("charge", invoice["id"], invoice["amount_cents"]))
    except (OSError, ValueError, RejectedError) as e:
        log_event("charge_failed", invoice=invoice["id"], error=repr(e))
        raise RasError("charge failed", cause=e, context={"invoice": invoice["id"]}) from e
