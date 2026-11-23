"""Negative control: retries the whole GET+POST sequence with no Idempotency-Key. With only two
attempts, a fault on every first attempt is spent on the GET; a fault on the POST alone shows the
duplicate (verify must reject)."""
import json
import os
import urllib.request

from ras_anything.runtime import RasError, is_transient, log_event, retry


def checkout(item):
    api = os.environ["CART_API"]

    def once():
        with urllib.request.urlopen(api + "/price/" + item, timeout=1) as r:
            price = json.loads(r.read())["cents"]
        req = urllib.request.Request(api + "/orders", data=json.dumps({"item": item, "cents": price}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1) as r:
            return json.loads(r.read())

    try:
        return retry(once, attempts=2, base=0.05, cap=0.2, budget_s=4,
                     retry_on=lambda e: is_transient(e) or isinstance(e, ValueError))
    except Exception as e:
        log_event("checkout_failed", item=item, error=repr(e))
        raise RasError("checkout failed", cause=e, context={"item": item}) from e
