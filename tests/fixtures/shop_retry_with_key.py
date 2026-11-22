"""Positive control: retries the POST with one deterministic Idempotency-Key."""
import json
import os
import urllib.request

from ras_anything.runtime import is_transient, retry


def place_order(item):
    def once():
        req = urllib.request.Request(os.environ["SHOP_API"] + "/orders", data=json.dumps({"item": item}).encode(),
                                     method="POST", headers={"Content-Type": "application/json",
                                                             "Idempotency-Key": "order-" + item})
        with urllib.request.urlopen(req, timeout=1) as r:
            return json.loads(r.read())
    return retry(once, attempts=5, base=0.05, cap=0.4, budget_s=4,
                 retry_on=lambda e: is_transient(e) or isinstance(e, ValueError))
