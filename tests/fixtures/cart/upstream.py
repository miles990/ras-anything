"""Test upstream: GET /price/<item> and POST /orders."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def _send(self, doc):
        body = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"cents": 100 + len(self.path)})

    def do_POST(self):
        doc = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        self._send({"order": doc["item"], "status": "placed"})

    def log_message(self, *a):
        pass


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1]
