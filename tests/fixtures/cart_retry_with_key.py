"""Positive control: retries each step on its own; every POST attempt carries one Idempotency-Key."""
import json
import os
import urllib.request

from ras_anything.runtime import RasError, is_transient, log_event, retry


def checkout(item):
    api = os.environ["CART_API"]

    def again(fn):
        return retry(fn, attempts=4, base=0.05, cap=0.2, budget_s=2.2,
                     retry_on=lambda e: is_transient(e) or isinstance(e, ValueError))

    def get_price():
        with urllib.request.urlopen(api + "/price/" + item, timeout=0.7) as r:
            return json.loads(r.read())["cents"]

    try:
        price = again(get_price)

        def post():
            req = urllib.request.Request(api + "/orders", data=json.dumps({"item": item, "cents": price}).encode(),
                                         method="POST", headers={"Content-Type": "application/json",
                                                                 "Idempotency-Key": "order-" + item})
            with urllib.request.urlopen(req, timeout=0.7) as r:
                return json.loads(r.read())
        return again(post)
    except Exception as e:
        log_event("checkout_failed", item=item, error=repr(e))
        raise RasError("checkout failed", cause=e, context={"item": item}) from e
