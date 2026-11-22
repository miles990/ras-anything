"""Nightly billing run: charge a batch of invoices, render receipts, reconcile with the ledger.

Runs its own local payment API in a thread (upstream.py) so it works offline.
"""

import json
import os
import urllib.request

import billing
import upstream

CUSTOMERS = ["Acme Co", "Globex", "Initech", "Umbrella", "Hooli", "Stark Ind", "Wayne Ent", "Wonka"]


def invoices():
    return [{"id": "INV-%04d" % (1001 + i), "customer": CUSTOMERS[i % len(CUSTOMERS)],
             "amount_cents": 1500 + (i * 737) % 9000, "currency": "usd"} for i in range(16)]


def main():
    os.environ["PAYMENTS_API_URL"] = upstream.start()
    batch = invoices()
    paid = 0
    print("BILLING RUN  %d invoices" % len(batch))
    for inv in batch:
        record = billing.charge(inv)
        if record is None:
            print(f"  {inv['id']}  {inv['amount_cents'] / 100:>8.2f}  PENDING (payment failed, retry tomorrow)")
            continue
        receipt = billing.render_receipt(inv, record)
        paid += 1
        print(f"  {inv['id']}  {inv['amount_cents'] / 100:>8.2f}  paid {record['id']}  receipt {receipt['bytes']}B {receipt['sha256'][:8]}")
    with urllib.request.urlopen(os.environ["PAYMENTS_API_URL"] + "/v1/ledger", timeout=5) as r:
        ledger = json.loads(r.read())
    print(f"{paid}/{len(batch)} paid · ledger: {ledger['charges']} charges, {ledger['duplicates']} duplicate, "
          f"total {ledger['total_cents'] / 100:.2f} USD")


if __name__ == "__main__":
    main()
