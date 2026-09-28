from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT=int(os.getenv("PORT","8080"))
API_KEY=os.getenv("BINANCE_API_KEY","").strip()
API_SECRET=os.getenv("BINANCE_API_SECRET","").strip()
BASE=os.getenv("BINANCE_PRIVATE_BASE_URL","https://api.binance.com").strip().rstrip("/")


def signed_account():
    if not API_KEY or not API_SECRET:
        raise RuntimeError("BINANCE_CREDENTIALS_MISSING")
    params={"omitZeroBalances":"true","recvWindow":"5000","timestamp":str(int(time.time()*1000))}
    query=urllib.parse.urlencode(params)
    sig=hmac.new(API_SECRET.encode(),query.encode(),hashlib.sha256).hexdigest()
    req=urllib.request.Request(
        f"{BASE}/api/v3/account?{query}&signature={sig}",
        headers={"X-MBX-APIKEY":API_KEY,"User-Agent":"tst-railway-readonly-probe/1.0"},
    )
    with urllib.request.urlopen(req,timeout=12) as r:
        return json.loads(r.read() or b"{}")


class H(BaseHTTPRequestHandler):
    def reply(self,status,payload):
        raw=json.dumps(payload,separators=(",",":")).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.split("?",1)[0]!="/health":
            return self.reply(404,{"ok":False,"status":"NOT_FOUND"})
        try:
            a=signed_account()
            return self.reply(200,{
                "ok":bool(a.get("canTrade")),
                "status":"BINANCE_READONLY_PREFLIGHT_OK" if a.get("canTrade") else "BINANCE_ACCOUNT_TRADING_BLOCKED",
                "canTrade":bool(a.get("canTrade")),
                "accountType":a.get("accountType"),
                "financialAction":False,
                "noBalanceValuesExposed":True,
                "noSecretValuesExposed":True,
            })
        except urllib.error.HTTPError as exc:
            try:
                row=json.loads(exc.read() or b"{}")
            except Exception:
                row={}
            return self.reply(503,{
                "ok":False,
                "status":"BINANCE_READONLY_PREFLIGHT_FAILED",
                "httpStatus":exc.code,
                "binanceCode":row.get("code"),
                "financialAction":False,
                "noSecretValuesExposed":True,
            })
        except Exception as exc:
            return self.reply(503,{
                "ok":False,
                "status":"BINANCE_READONLY_PREFLIGHT_FAILED",
                "reason":type(exc).__name__,
                "financialAction":False,
                "noSecretValuesExposed":True,
            })

    def do_POST(self):
        return self.reply(405,{"ok":False,"status":"READ_ONLY_PROBE","financialAction":False})

    def log_message(self,*_):
        return


if __name__=="__main__":
    print(f"[railway-readonly-probe] ONLINE configured={bool(API_KEY and API_SECRET)}",flush=True)
    ThreadingHTTPServer(("0.0.0.0",PORT),H).serve_forever()
