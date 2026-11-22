"""Test fixture: a correct hardening of examples/payments charge (positive control for tests)."""

import json
import os
import urllib.request

from ras_anything.runtime import RasError, is_transient, log_event, retry

RAS_FALLBACK = None


def charge(invoice):
    api = os.environ.get("PAYMENTS_API_URL", "https://api.payments.example")
    payload = json.dumps({"invoice": invoice["id"], "amount_cents": invoice["amount_cents"],
                          "currency": invoice["currency"]}).encode()
    key = "charge-%s-%d" % (invoice["id"], invoice["amount_cents"])

    def once():
        req = urllib.request.Request(f"{api}/v1/charges", data=payload, method="POST",
                                     headers={"Content-Type": "application/json", "Idempotency-Key": key})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return json.loads(resp.read())

    try:
        return retry(once, attempts=5, base=0.1, cap=0.8, budget_s=4.0,
                     retry_on=lambda e: is_transient(e) or isinstance(e, ValueError))
    except Exception as e:
        log_event("charge_failed", invoice=invoice["id"], error=repr(e))
        raise RasError("charge failed", cause=e, context={"invoice": invoice["id"]}) from e
