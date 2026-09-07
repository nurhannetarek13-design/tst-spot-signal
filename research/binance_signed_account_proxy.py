import json
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", os.environ.get("PROXY_PORT", "8080")))
# BINANCE_DEMO_* credentials belong to Binance Spot Demo Mode, not the legacy Spot Testnet.
# Keep the external read-only relay contract stable while routing to the official Demo REST API.
UPSTREAM = "https://demo-api.binance.com/api/v3/account"
MAX_BODY = 8192


def send_json(handler, status, payload):
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("content-length", str(len(data)))
    handler.send_header("cache-control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def safe_query(query):
    if not isinstance(query, str) or not query or len(query) > 4000:
        return False, "BAD_QUERY"
    parsed = urllib.parse.parse_qs(query, keep_blank_values=True)
    try:
        timestamp = int((parsed.get("timestamp") or [""])[0])
        recv_window = int((parsed.get("recvWindow") or ["5000"])[0])
    except ValueError:
        return False, "BAD_TIMESTAMP"
    signature = (parsed.get("signature") or [""])[0]
    if len(signature) != 64 or any(c not in "0123456789abcdefABCDEF" for c in signature):
        return False, "BAD_SIGNATURE_FORMAT"
    max_age = max(60000, recv_window + 10000)
    if abs(int(time.time() * 1000) - timestamp) > max_age:
        return False, "STALE_REQUEST"
    return True, None


def safe_msg(value):
    return str(value or "")[:240]


class Handler(BaseHTTPRequestHandler):
    server_version = "tst-binance-readonly-relay/1.2"

    def do_GET(self):
        if self.path.startswith("/health"):
            return send_json(self, 200, {
                "ok": True,
                "mode": "READ_ONLY_TESTNET_ACCOUNT",
                "upstreamMode": "BINANCE_SPOT_DEMO",
            })
        return send_json(self, 404, {"ok": False, "status": "NOT_FOUND"})

    def do_POST(self):
        if not self.path.startswith("/signed-testnet-account"):
            return send_json(self, 404, {"ok": False, "status": "NOT_FOUND"})
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return send_json(self, 400, {"ok": False, "status": "BAD_BODY_SIZE"})
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return send_json(self, 400, {"ok": False, "status": "BAD_JSON"})

        api_key = str(body.get("apiKey") or "")
        query = str(body.get("query") or "")
        if not (20 <= len(api_key) <= 256) or not all(c.isalnum() or c in "_-" for c in api_key):
            return send_json(self, 400, {"ok": False, "status": "BAD_API_KEY"})
        ok, reason = safe_query(query)
        if not ok:
            return send_json(self, 400, {"ok": False, "status": reason})

        request = urllib.request.Request(
            UPSTREAM + "?" + query,
            method="GET",
            headers={
                "X-MBX-APIKEY": api_key,
                "Accept": "application/json",
                "User-Agent": "tst-railway-readonly-relay/1.2",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                raw = response.read().decode("utf-8", errors="replace")
                try:
                    upstream = json.loads(raw or "{}")
                except Exception:
                    upstream = {"raw": raw[:500]}
                return send_json(self, 200, {
                    "ok": True,
                    "network": "testnet",
                    "upstreamMode": "BINANCE_SPOT_DEMO",
                    "data": upstream,
                })
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                upstream = json.loads(raw or "{}")
            except Exception:
                upstream = {"msg": raw[:500]}
            payload = {
                "ok": False,
                "network": "testnet",
                "upstreamMode": "BINANCE_SPOT_DEMO",
                "upstream": {
                    "status": exc.code,
                    "code": upstream.get("code"),
                    "msg": safe_msg(upstream.get("msg", "upstream error")),
                },
            }
            print(json.dumps({
                "kind": "binance_readonly_upstream_error",
                "upstreamMode": "BINANCE_SPOT_DEMO",
                "status": exc.code,
                "code": upstream.get("code"),
                "msg": safe_msg(upstream.get("msg", "upstream error")),
            }), flush=True)
            return send_json(self, 200, payload)
        except Exception as exc:
            payload = {
                "ok": False,
                "status": "UPSTREAM_UNAVAILABLE",
                "upstreamMode": "BINANCE_SPOT_DEMO",
                "reason": safe_msg(exc),
            }
            print(json.dumps({
                "kind": "binance_readonly_transport_error",
                "upstreamMode": "BINANCE_SPOT_DEMO",
                "reason": safe_msg(exc),
            }), flush=True)
            return send_json(self, 200, payload)

    def log_message(self, fmt, *args):
        print(json.dumps({"kind": "binance_readonly_relay", "message": fmt % args}), flush=True)


if __name__ == "__main__":
    print(json.dumps({
        "kind": "binance_readonly_relay_start",
        "port": PORT,
        "mode": "READ_ONLY_TESTNET_ACCOUNT",
        "upstreamMode": "BINANCE_SPOT_DEMO",
    }), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
