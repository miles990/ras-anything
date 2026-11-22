"""A local stand-in for a public forecast API, so the example runs offline.

Started in a background thread by app.py. It is healthy: every fault you see during
`ras-anything scan`/`bench` is injected at the app's side of the network boundary.
"""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Cities the provider has taken offline for maintenance (documented API behaviour).
MAINTENANCE = {"Reykjavik"}
DAYS = ("Mon", "Tue", "Wed")


def forecast_for(city):
    h = hashlib.sha256(city.encode()).digest()
    daily = []
    for i, day in enumerate(DAYS):
        hi = 8 + h[i] % 25
        daily.append({"day": day, "hi_c": hi, "lo_c": hi - 4 - h[i + 3] % 7, "rain_mm": round(h[i + 6] % 90 / 10, 1)})
    return {"city": city, "daily": daily, "source": "local-upstream"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/v1/forecast":
            self.send_error(404)
            return
        city = parse_qs(u.query).get("city", [""])[0]
        if not city:
            self.send_error(400)
            return
        doc = {"city": city, "status": "unavailable"} if city in MAINTENANCE else forecast_for(city)
        body = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1]
