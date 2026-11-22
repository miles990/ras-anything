"""Test upstream: an orders API that counts every POST it receives (no idempotency support needed)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    n = 0

    def do_POST(self):
        doc = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        Handler.n += 1
        body = json.dumps({"order": doc["item"], "status": "placed"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1]
