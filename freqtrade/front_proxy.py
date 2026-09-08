from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT=int(os.getenv('PORT','8080'))
BRIDGE_PORT=int(os.getenv('BRIDGE_PORT','8082'))
SIGNER_PORT=int(os.getenv('SIGNER_PORT','8081'))
EXECUTOR_PORT=int(os.getenv('EXECUTOR_PORT','8083'))
MAKE_WEBHOOK_URL=(os.getenv('MAKE_ONE_TAP_WEBHOOK_URL') or '').strip()
TELEGRAM_BOT_TOKEN=(os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()


def _json_bytes(payload):
    return json.dumps(payload,separators=(',',':'),ensure_ascii=False).encode('utf-8')


class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'

    def send_json(self,status,payload):
        data=_json_bytes(payload)
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Connection','close')
        self.end_headers()
        self.wfile.write(data)

    def make_exec_relay(self):
        if self.command!='POST':
            return self.send_json(405,{'ok':False,'status':'METHOD_NOT_ALLOWED'})
        if not MAKE_WEBHOOK_URL or not TELEGRAM_BOT_TOKEN:
            return self.send_json(503,{'ok':False,'status':'MAKE_RELAY_NOT_CONFIGURED'})
        length=int(self.headers.get('Content-Length') or 0)
        if length<=0 or length>8192:
            return self.send_json(400,{'ok':False,'status':'BAD_BODY'})
        raw=self.rfile.read(length)
        ts=str(self.headers.get('X-Make-Relay-Timestamp') or '')
        supplied=str(self.headers.get('X-Make-Relay-Signature') or '').lower()
        try:
            stamp=int(ts)
        except Exception:
            return self.send_json(401,{'ok':False,'status':'BAD_TIMESTAMP'})
        if abs(int(time.time()*1000)-stamp)>60000:
            return self.send_json(401,{'ok':False,'status':'STALE_RELAY'})
        expected=hmac.new(TELEGRAM_BOT_TOKEN.encode('utf-8'),ts.encode('utf-8')+b'.'+raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied,expected):
            return self.send_json(401,{'ok':False,'status':'BAD_RELAY_SIGNATURE'})
        try:
            body=json.loads(raw or b'{}')
        except Exception:
            return self.send_json(400,{'ok':False,'status':'BAD_JSON'})
        symbol=str(body.get('symbol') or '').upper()
        quote=float(body.get('quote_amount_usdt') or 0)
        if body.get('confirmed') is not True or body.get('dry_run') is not False:
            return self.send_json(409,{'ok':False,'status':'CONFIRMATION_REQUIRED'})
        if str(body.get('action') or '').upper()!='BUY':
            return self.send_json(400,{'ok':False,'status':'BAD_ACTION'})
        if not re.fullmatch(r'[A-Z0-9]{1,20}USDT',symbol):
            return self.send_json(400,{'ok':False,'status':'BAD_SYMBOL'})
        if not (5<=quote<=10):
            return self.send_json(400,{'ok':False,'status':'BAD_STAKE'})
        req=urllib.request.Request(
            MAKE_WEBHOOK_URL,
            data=raw,
            method='POST',
            headers={'Content-Type':'application/json','Cache-Control':'no-store','User-Agent':'tst-make-relay/1.0'},
        )
        try:
            with urllib.request.urlopen(req,timeout=45) as r:
                data=r.read()
                status=r.status
        except urllib.error.HTTPError as exc:
            status=exc.code
            data=exc.read() or b'{}'
        except Exception as exc:
            print(f'[make-relay] transport failed: {type(exc).__name__}: {str(exc)[:160]}',flush=True)
            return self.send_json(502,{'ok':False,'status':'MAKE_TRANSPORT_FAILED'})
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Connection','close')
        self.end_headers()
        self.wfile.write(data)

    def proxy(self):
        if self.path.startswith('/make-exec-relay'):
            return self.make_exec_relay()
        if self.path.startswith('/signer/'):
            port=SIGNER_PORT; path=self.path[len('/signer'):]
        elif self.path.startswith('/execute'):
            port=EXECUTOR_PORT; path=self.path[len('/execute'):] or '/'
        else:
            port=BRIDGE_PORT; path=self.path
        length=int(self.headers.get('Content-Length') or 0)
        body=self.rfile.read(length) if length else None
        headers={k:v for k,v in self.headers.items() if k.lower() not in {'host','content-length','connection'}}
        try:
            c=http.client.HTTPConnection('127.0.0.1',port,timeout=45)
            c.request(self.command,path,body=body,headers=headers)
            r=c.getresponse(); data=r.read()
            self.send_response(r.status)
            for k,v in r.getheaders():
                if k.lower() not in {'connection','transfer-encoding','content-length'}: self.send_header(k,v)
            self.send_header('Content-Length',str(len(data))); self.send_header('Connection','close'); self.end_headers(); self.wfile.write(data); c.close()
        except Exception as e:
            data=f'upstream unavailable: {type(e).__name__}'.encode(); self.send_response(503); self.send_header('Content-Type','text/plain'); self.send_header('Content-Length',str(len(data))); self.send_header('Connection','close'); self.end_headers(); self.wfile.write(data)
    do_GET=proxy
    do_POST=proxy
    def log_message(self,*_): pass

if __name__=='__main__':
    print(f'[front-proxy] ONLINE external={PORT} bridge={BRIDGE_PORT} signer={SIGNER_PORT} executor={EXECUTOR_PORT} make_relay={bool(MAKE_WEBHOOK_URL)}', flush=True)
    ThreadingHTTPServer(('0.0.0.0',PORT),H).serve_forever()
