import json
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALLOWED = {
    "/api/v3/ticker/24hr",
    "/api/v3/ticker/bookTicker",
    "/api/v3/exchangeInfo",
    "/api/v3/klines",
    "/api/v3/depth",
    "/api/v3/trades",
    "/api/v3/aggTrades",
}
BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api-gcp.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
]

class Handler(BaseHTTPRequestHandler):
    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("cache-control", "public, max-age=1")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        req = urllib.parse.urlsplit(self.path)
        if req.path == "/health":
            return self._json(200, {"ok": True, "service": "binance-public-proxy"})
        if req.path != "/api/binance-public":
            return self._json(404, {"ok": False, "error": "NOT_FOUND"})
        q = urllib.parse.parse_qs(req.query)
        raw = (q.get("path") or [""])[0]
        decoded = urllib.parse.unquote(raw)
        upstream = urllib.parse.urlsplit("https://local" + decoded)
        if upstream.path not in ALLOWED:
            return self._json(403, {"ok": False, "error": "PATH_NOT_ALLOWED"})
        last = "unavailable"
        for base in BASES:
            try:
                url = base + upstream.path + (("?" + upstream.query) if upstream.query else "")
                r = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "tst-railway-binance-proxy/2.0"})
                with urllib.request.urlopen(r, timeout=12) as resp:
                    data = resp.read()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("cache-control", "public, max-age=1")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            except Exception as exc:
                last = str(exc)
        return self._json(502, {"ok": False, "error": "BINANCE_UPSTREAM_FAILED", "detail": last})

    def log_message(self, fmt, *args):
        print(json.dumps({"kind": "binance_public_proxy", "message": fmt % args}), flush=True)

if __name__ == "__main__":
    port = int(os.environ.get("PROXY_PORT", "8080"))
    print(json.dumps({"kind": "binance_public_proxy_start", "port": port}), flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
