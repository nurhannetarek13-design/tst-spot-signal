from __future__ import annotations

import http.client, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT=int(os.getenv('PORT','8080'))
BRIDGE_PORT=int(os.getenv('BRIDGE_PORT','8082'))
SIGNER_PORT=int(os.getenv('SIGNER_PORT','8081'))
EXECUTOR_PORT=int(os.getenv('EXECUTOR_PORT','8083'))

class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def proxy(self):
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
