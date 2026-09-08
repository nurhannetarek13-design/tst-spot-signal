from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT=int(os.getenv('PORT','8080'))
BRIDGE_PORT=int(os.getenv('BRIDGE_PORT','8082'))
SIGNER_PORT=int(os.getenv('SIGNER_PORT','8081'))
EXECUTOR_PORT=int(os.getenv('EXECUTOR_PORT','8083'))
SCRIPT_NAME='tst-spot-signal'


def clean_env(name):
    v=(os.getenv(name) or '').strip()
    if len(v)>=2 and v[0]==v[-1] and v[0] in ('"', "'"):
        v=v[1:-1].strip()
    return v


def cf_put_secret(account_id, token, name, value):
    url=f'https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{SCRIPT_NAME}/secrets'
    raw=json.dumps({'name':name,'text':value,'type':'secret_text'},separators=(',',':')).encode()
    req=urllib.request.Request(url,data=raw,method='PUT',headers={
        'Authorization':f'Bearer {token}',
        'Content-Type':'application/json',
        'User-Agent':'tst-secret-sync/1.0',
    })
    try:
        with urllib.request.urlopen(req,timeout=20) as r:
            body=json.loads(r.read() or b'{}')
            return bool(body.get('success'))
    except urllib.error.HTTPError as exc:
        try:
            body=json.loads(exc.read() or b'{}')
            errs=body.get('errors') or []
            msg='; '.join(str(x.get('message') or x.get('code') or 'error') for x in errs[:3])
        except Exception:
            msg=f'HTTP {exc.code}'
        raise RuntimeError(f'Cloudflare secret update failed: {msg[:180]}')


class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'

    def send_json(self,status,payload):
        data=json.dumps(payload,separators=(',',':')).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Connection','close')
        self.end_headers()
        self.wfile.write(data)

    def sync_cloudflare(self):
        if self.command!='POST':
            return self.send_json(405,{'ok':False,'status':'METHOD_NOT_ALLOWED'})
        length=int(self.headers.get('Content-Length') or 0)
        if length<=0 or length>8192:
            return self.send_json(400,{'ok':False,'status':'BAD_BODY'})
        try:
            body=json.loads(self.rfile.read(length) or b'{}')
        except Exception:
            return self.send_json(400,{'ok':False,'status':'BAD_JSON'})
        account_id=str(body.get('accountId') or '').strip()
        cf_token=str(body.get('apiToken') or '').strip()
        if not (10<=len(account_id)<=64 and 20<=len(cf_token)<=256):
            return self.send_json(401,{'ok':False,'status':'AUTH_REQUIRED'})
        api_key=clean_env('BINANCE_API_KEY')
        api_secret=clean_env('BINANCE_API_SECRET')
        if not api_key or not api_secret:
            return self.send_json(503,{'ok':False,'status':'RAILWAY_BINANCE_CREDENTIALS_MISSING'})
        try:
            ok1=cf_put_secret(account_id,cf_token,'BINANCE_API_KEY',api_key)
            ok2=cf_put_secret(account_id,cf_token,'BINANCE_API_SECRET',api_secret)
            if not (ok1 and ok2):
                raise RuntimeError('Cloudflare API did not confirm secret updates')
            print('[secret-sync] Cloudflare live Binance secrets synchronized successfully',flush=True)
            return self.send_json(200,{
                'ok':True,
                'status':'CLOUDFLARE_BINANCE_SECRETS_SYNCED',
                'keyLength':len(api_key),
                'secretLength':len(api_secret),
                'valuesExposed':False,
            })
        except Exception as exc:
            print(f'[secret-sync] failed: {type(exc).__name__}: {str(exc)[:180]}',flush=True)
            return self.send_json(502,{'ok':False,'status':'CLOUDFLARE_SYNC_FAILED','reason':str(exc)[:180]})

    def proxy(self):
        if self.path.startswith('/admin/sync-cloudflare-binance'):
            return self.sync_cloudflare()
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
    print(f'[front-proxy] ONLINE external={PORT} bridge={BRIDGE_PORT} signer={SIGNER_PORT} executor={EXECUTOR_PORT}', flush=True)
    ThreadingHTTPServer(('0.0.0.0',PORT),H).serve_forever()
