"""billing.charge on a Policy, for the pattern lab: the marked line is replaced per variant."""

import json
import os
import urllib.request

from ras_anything.runtime import Policy, idempotency_key, is_transient

RAS_FALLBACK = lambda *a, **k: None  # noqa: E731
retryable = lambda e: is_transient(e) or isinstance(e, ValueError)  # noqa: E731
BASE = {"deadline": {"timeout_s": 4.7}, "attempt_timeout": {"timeout_s": 3.4}, "retry": {"attempts": 4, "retry_on": retryable},
        "fallback": {"degraded": lambda e: None, "when": retryable}, "hedge": {"delay_s": 0.5}}
PATTERNS = {}  # ras-lab

POLICY = {k: {**BASE.get(k, {}), **(v if isinstance(v, dict) else {})} if v else False for k, v in PATTERNS.items()}
payments = Policy("payments-api", **POLICY)


def charge(invoice):
    api = os.environ.get("PAYMENTS_API_URL", "https://api.payments.example")
    payload = json.dumps({"invoice": invoice["id"], "amount_cents": invoice["amount_cents"], "currency": invoice["currency"]}).encode()

    def attempt(a):
        req = urllib.request.Request(f"{api}/v1/charges", data=payload, method="POST", headers={"Content-Type": "application/json", **a.headers})
        with urllib.request.urlopen(req, timeout=a.timeout_s) as r:
            return json.loads(r.read())

    return payments.call(attempt, method="POST", idempotency_key=idempotency_key("charge", invoice["id"], invoice["amount_cents"]))
