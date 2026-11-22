"""Negative control: adds retries to a POST without an Idempotency-Key (verify must reject: duplicates)."""
import json
import os
import urllib.request

from ras_anything.runtime import retry


def place_order(item):
    def once():
        req = urllib.request.Request(os.environ["SHOP_API"] + "/orders", data=json.dumps({"item": item}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1) as r:
            return json.loads(r.read())
    return retry(once, attempts=4, base=0.05, cap=0.4, budget_s=4)
