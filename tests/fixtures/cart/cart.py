import json
import os
import urllib.request


def checkout(item):
    """Look the price up (GET), then place the order at that price (POST)."""
    api = os.environ["CART_API"]
    with urllib.request.urlopen(api + "/price/" + item, timeout=2) as r:
        price = json.loads(r.read())["cents"]
    req = urllib.request.Request(api + "/orders", data=json.dumps({"item": item, "cents": price}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=2) as r:
        return json.loads(r.read())
