from __future__ import annotations

import base64, hashlib, hmac, json, os, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

API_KEY = os.getenv('BINANCE_API_KEY', '').strip()
API_SECRET = os.getenv('BINANCE_API_SECRET', '').strip()
MAX_QUOTE = float(os.getenv('MAX_QUOTE_USDT', '10'))
PORT = int(os.getenv('SIGNER_PORT', '8081'))

def key(): return hashlib.sha256(f'tst-executor-v1:{API_SECRET}'.encode()).digest()
def decode_token(token):
    try:
        payload, mac = token.rsplit('.',1)
        expected = hmac.new(key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(mac, expected): return None
        data=json.loads(base64.urlsafe_b64decode((payload+'='*(-len(payload)%4)).encode()).decode())
        if time.time()>float(data.get('exp') or 0): return None
        pair=str(data.get('pair') or ''); stake=float(data.get('stake_usdt') or 0); entry=float(data.get('entry') or 0); tp=float(data.get('tp') or 0); sl=float(data.get('sl') or 0)
        if not(pair.endswith('/USDT') and 0<stake<=MAX_QUOTE and entry>0 and tp>entry and 0<sl<entry): return None
        return data
    except Exception: return None

def spec(path, method, params):
    pairs=[(k,str(v)) for k,v in params.items() if v is not None and v!='']+[('recvWindow','5000'),('timestamp',str(int(time.time()*1000)))]
    query=urlencode(pairs); signature=hmac.new(API_SECRET.encode(),query.encode(),hashlib.sha256).hexdigest()
    return {'apiKey':API_KEY,'path':path,'method':method,'query':query,'signature':signature,'expiresMs':int(time.time()*1000)+5000}

def sid(sig): return ''.join(c for c in str(sig['id']) if c.isalnum())[:20]
def symbol(sig): return sig['pair'].replace('/','')

class H(BaseHTTPRequestHandler):
    def out(self,status,data):
        raw=json.dumps(data,separators=(',',':')).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        u=urlparse(self.path)
        if u.path=='/health': return self.out(200,{'ok':True,'configured':bool(API_KEY and API_SECRET)})
        if u.path!='/validate': return self.out(404,{'ok':False,'error':'not found'})
        sig=decode_token(parse_qs(u.query).get('t',[''])[0])
        if not sig: return self.out(400,{'ok':False,'error':'invalid or expired authorization'})
        safe={k:sig.get(k) for k in ('id','pair','stake_usdt','entry','tp','sl','iat','exp')}; return self.out(200,{'ok':True,'signal':safe})
    def do_POST(self):
        if urlparse(self.path).path!='/sign': return self.out(404,{'ok':False,'error':'not found'})
        try: body=json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        except Exception: return self.out(400,{'ok':False,'error':'invalid json'})
        sig=decode_token(str(body.get('t') or ''))
        if not sig: return self.out(403,{'ok':False,'error':'invalid or expired authorization'})
        op=str(body.get('op') or ''); sym=symbol(sig); ident=sid(sig)
        try:
            if op=='account': s=spec('/api/v3/account','GET',{'omitZeroBalances':'true'})
            elif op=='query_buy': s=spec('/api/v3/order','GET',{'symbol':sym,'origClientOrderId':f'tstb_{ident}'})
            elif op=='buy': s=spec('/api/v3/order','POST',{'symbol':sym,'side':'BUY','type':'MARKET','quoteOrderQty':f"{min(float(sig['stake_usdt']),MAX_QUOTE):.2f}",'newClientOrderId':f'tstb_{ident}','newOrderRespType':'FULL'})
            elif op=='query_oco': s=spec('/api/v3/orderList','GET',{'origClientOrderId':f'tsto_{ident}'})
            elif op=='oco':
                qty=float(body.get('quantity') or 0); tp=float(body.get('tp') or 0); st=float(body.get('slTrigger') or 0); sl=float(body.get('slLimit') or 0); entry=float(sig['entry']); maxqty=float(sig['stake_usdt'])/entry*1.35
                if not(0<qty<=maxqty and tp>st>sl>0 and tp<=entry*1.15 and sl>=entry*0.80): raise ValueError('OCO outside authorized safety envelope')
                s=spec('/api/v3/orderList/oco','POST',{'symbol':sym,'side':'SELL','quantity':body.get('quantity'),'listClientOrderId':f'tsto_{ident}','aboveType':'LIMIT_MAKER','abovePrice':body.get('tp'),'belowType':'STOP_LOSS_LIMIT','belowStopPrice':body.get('slTrigger'),'belowPrice':body.get('slLimit'),'belowTimeInForce':'GTC','newOrderRespType':'RESULT'})
            else: return self.out(400,{'ok':False,'error':'unsupported op'})
            return self.out(200,{'ok':True,'spec':s})
        except Exception as e: return self.out(400,{'ok':False,'error':str(e)[:300]})
    def log_message(self,*_): pass

if __name__=='__main__': HTTPServer(('0.0.0.0',PORT),H).serve_forever()
