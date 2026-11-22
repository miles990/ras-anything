"""Receipt renderer, run as a separate process (like a PDF tool would be).

Usage: python render.py '<receipt json>'  ->  prints {"pages", "bytes", "sha256"}
"""

import hashlib
import json
import sys

receipt = json.loads(sys.argv[1])
doc = "\n".join([
    "RECEIPT",
    "invoice  %s" % receipt["invoice"],
    "customer %s" % receipt["customer"],
    "amount   %.2f %s" % (receipt["amount_cents"] / 100, receipt["currency"].upper()),
    "charge   %s" % receipt["charge_id"],
]).encode()
print(json.dumps({"pages": 1, "bytes": len(doc), "sha256": hashlib.sha256(doc).hexdigest()}))
