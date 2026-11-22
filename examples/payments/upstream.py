"""A local stand-in for a payment provider, so the example runs offline.

Started in a background thread by app.py. Like real providers it honours an
`Idempotency-Key` header: a repeated key returns the original charge instead of charging
again. Without one, every POST that reaches it is a new charge. GET /v1/ledger reports
how many charges exist per invoice, so duplicate charges show up in the app's output.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Ledger:
    def __init__(self):
        self.lock = threading.Lock()
        self.charges = []
        self.by_key = {}


class Handler(BaseHTTPRequestHandler):
    ledger = None

    def _send(self, code, doc):
        body = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/v1/charges":
            return self._send(404, {"error": "not found"})
        doc = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if not doc.get("invoice") or not isinstance(doc.get("amount_cents"), int) or doc["amount_cents"] <= 0:
            return self._send(400, {"error": "invalid charge"})
        key = self.headers.get("Idempotency-Key")
        led = self.ledger
        with led.lock:
            if key and key in led.by_key:
                return self._send(200, led.by_key[key])
            charge = {"id": "ch_%04d" % (len(led.charges) + 1), "invoice": doc["invoice"],
                      "amount_cents": doc["amount_cents"], "currency": doc.get("currency", "usd"), "status": "succeeded"}
            led.charges.append(charge)
            if key:
                led.by_key[key] = charge
        self._send(200, charge)

    def do_GET(self):
        if self.path != "/v1/ledger":
            return self._send(404, {"error": "not found"})
        led = self.ledger
        with led.lock:
            per = {}
            for c in led.charges:
                per[c["invoice"]] = per.get(c["invoice"], 0) + 1
            total = sum(c["amount_cents"] for c in led.charges)
        self._send(200, {"charges": len(led.charges), "invoices": len(per),
                         "duplicates": sum(n - 1 for n in per.values()), "total_cents": total})

    def log_message(self, *args):
        pass


def start():
    Handler.ledger = Ledger()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1]
