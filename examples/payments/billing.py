"""Billing: charge invoices through the payment API and render receipts."""

import json
import os
import subprocess
import sys
import time
import urllib.request

RENDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render.py")


def charge(invoice):
    """Charge one invoice. Returns the provider's charge record, or None if it didn't go through."""
    api = os.environ.get("PAYMENTS_API_URL", "https://api.payments.example")
    payload = json.dumps({"invoice": invoice["id"], "amount_cents": invoice["amount_cents"],
                          "currency": invoice["currency"]}).encode()
    for attempt in range(3):  # the provider is flaky, so try a few times
        try:
            req = urllib.request.Request(f"{api}/v1/charges", data=payload, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception:
            time.sleep(0.5)
    return None


def render_receipt(invoice, charge_record):
    """Render the receipt in a separate process; returns its page count, size and hash."""
    receipt = {"invoice": invoice["id"], "customer": invoice["customer"], "amount_cents": invoice["amount_cents"],
               "currency": invoice["currency"], "charge_id": charge_record["id"]}
    out = subprocess.run([sys.executable, RENDER, json.dumps(receipt)], capture_output=True, text=True)
    return json.loads(out.stdout)
