import json
import os
import urllib.request


def place_order(item):
    req = urllib.request.Request(os.environ["SHOP_API"] + "/orders", data=json.dumps({"item": item}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=2) as r:
        return json.loads(r.read())
