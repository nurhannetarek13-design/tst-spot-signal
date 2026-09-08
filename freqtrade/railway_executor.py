from __future__ import annotations

import base64, hashlib, hmac, html, json, math, os, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT=int(os.getenv('EXECUTOR_PORT','8083'))
API_KEY=os.getenv('BINANCE_API_KEY','').strip()
API_SECRET=os.getenv('BINANCE_API_SECRET','').strip()
TG_TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
TG_CHAT=os.getenv('TELEGRAM_CHAT_ID','').strip()
RELAY=os.getenv('BINANCE_RELAY_URL','https://tst-spot-signal.vercel.app/api/binance-signed-relay').strip()
MAX_QUOTE=float(os.getenv('EXECUTOR_MAX_QUOTE_USDT','10'))

PUBLIC_BASES=['https://data-api.binance.vision','https://api.binance.com','https://api1.binance.com','https://api2.binance.com']

def executor_key(): return hashlib.sha256(f'tst-executor-v1:{API_SECRET}'.encode()).digest()
def decode_token(token):
    try:
        payload,mac=token.rsplit('.',1)
        exp=hmac.new(executor_key(),payload.encode(),hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(mac,exp): return None
        raw=base64.urlsafe_b64decode((payload+'='*(-len(payload)%4)).encode())
        d=json.loads(raw.decode())
        if time.time()>float(d.get('exp') or 0): return None
        pair=str(d.get('pair') or ''); stake=float(d.get('stake_usdt') or 0); entry=float(d.get('entry') or 0); tp=float(d.get('tp') or 0); sl=float(d.get('sl') or 0)
        if not(pair.endswith('/USDT') and 5<=stake<=MAX_QUOTE and entry>0 and tp>entry and 0<sl<entry): return None
        return d
    except Exception: return None

def sign_query(params):
    p=[(k,str(v)) for k,v in params.items() if v is not None and v!='']+[('recvWindow','5000'),('timestamp',str(int(time.time()*1000)))]
    qs=urllib.parse.urlencode(p); sig=hmac.new(API_SECRET.encode(),qs.encode(),hashlib.sha256).hexdigest(); return qs+'&signature='+sig

def relay(method,path,params):
    query=sign_query(params)
    body=json.dumps({'method':method,'path':path,'apiKey':API_KEY,'query':query,'network':'production'},separators=(',',':')).encode()
    ts=str(int(time.time()*1000)); rsig=hmac.new(TG_TOKEN.encode(),f'{ts}.{body.decode()}'.encode(),hashlib.sha256).hexdigest()
    req=urllib.request.Request(RELAY,data=body,headers={'Content-Type':'application/json','X-Executor-Timestamp':ts,'X-Executor-Signature':rsig,'User-Agent':'tst-railway-executor/1.0'},method='POST')
    with urllib.request.urlopen(req,timeout=20) as r: data=json.loads(r.read())
    if not data.get('ok'): raise RuntimeError(f"relay failed: {data.get('status')} {data.get('upstream') or data}")
    return data.get('data') or {}

def public(path):
    last='unknown'
    for b in PUBLIC_BASES:
        try:
            with urllib.request.urlopen(urllib.request.Request(b+path,headers={'User-Agent':'tst-railway-executor/1.0'}),timeout=12) as r: return json.loads(r.read())
        except Exception as e: last=str(e)
    raise RuntimeError(f'public Binance unavailable: {last}')

def dec(step):
    s=f'{step:.16f}'.rstrip('0')
    return len(s.split('.')[1]) if '.' in s else 0

def floor_step(v,step):
    d=dec(step); return f'{math.floor((v+1e-12)/step)*step:.{d}f}'

def tick_round(v,tick,mode):
    n=v/tick
    k=math.floor(n+1e-12) if mode=='down' else math.ceil(n-1e-12)
    d=dec(tick); return f'{k*tick:.{d}f}'

def market_info(symbol):
    x=public('/api/v3/exchangeInfo?symbol='+urllib.parse.quote(symbol)); s=(x.get('symbols') or [None])[0]
    if not s or s.get('status')!='TRADING' or not s.get('isSpotTradingAllowed'): raise RuntimeError('pair not Spot TRADING')
    fs={f.get('filterType'):f for f in s.get('filters') or []}; lot=fs.get('LOT_SIZE',{}); pf=fs.get('PRICE_FILTER',{}); nt=fs.get('NOTIONAL') or fs.get('MIN_NOTIONAL') or {}
    return float(lot.get('stepSize') or 1e-8),float(lot.get('minQty') or 0),float(pf.get('tickSize') or 1e-8),float(nt.get('minNotional') or 5)

def tg(text):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        data=urllib.parse.urlencode({'chat_id':TG_CHAT,'text':text,'disable_web_page_preview':'true'}).encode()
        urllib.request.urlopen(urllib.request.Request(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',data=data),timeout=10).read()
    except Exception: pass

def page(title,body):
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>*{{box-sizing:border-box}}body{{margin:0;background:#0b0e11;color:#eaecef;font-family:Arial;padding:18px}}.card{{max-width:520px;margin:20px auto;background:#181a20;border:1px solid #2b3139;border-radius:18px;padding:22px}}.badge{{display:inline-block;padding:6px 10px;border-radius:8px;background:#2b3139;color:#fcd535;font-size:12px;font-weight:800}}h1{{font-size:25px}}.row{{display:flex;justify-content:space-between;padding:11px 0;border-bottom:1px solid #2b3139;gap:12px}}.label{{color:#848e9c}}.val{{font-weight:800;text-align:right}}.green{{color:#0ecb81}}.red{{color:#f6465d}}.warn{{margin:16px 0;padding:12px;background:#252a31;border-radius:10px;line-height:1.5;font-size:13px}}button{{width:100%;border:0;border-radius:11px;padding:16px;background:#fcd535;color:#181a20;font-weight:900;font-size:16px}}.bad{{color:#f6465d}}.ok{{color:#0ecb81}}</style></head><body><div class="card">{body}</div></body></html>'''

class H(BaseHTTPRequestHandler):
    def send_html(self,status,s):
        b=s.encode(); self.send_response(status); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def send_json(self,status,d):
        b=json.dumps(d).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u=urllib.parse.urlparse(self.path)
        if u.path=='/preflight':
            try:
                a=relay('GET','/api/v3/account',{'omitZeroBalances':'true'}); return self.send_json(200,{'ok':True,'canTrade':bool(a.get('canTrade')),'accountType':a.get('accountType')})
            except Exception as e: return self.send_json(503,{'ok':False,'error':str(e)[:300]})
        if u.path!='/': return self.send_json(404,{'ok':False})
        t=urllib.parse.parse_qs(u.query).get('t',[''])[0]; sig=decode_token(t)
        if not sig: return self.send_html(400,page('Invalid','<div class="badge">INVALID / EXPIRED</div><h1 class="bad">Signal invalid or expired</h1>'))
        try: a=relay('GET','/api/v3/account',{'omitZeroBalances':'true'}); can=bool(a.get('canTrade'))
        except Exception: can=False
        stake=min(float(sig['stake_usdt']),MAX_QUOTE); entry=float(sig['entry']); tp=float(sig['tp']); sl=float(sig['sl'])
        status='<div class="badge">ACCOUNT PREFLIGHT ✓</div>' if can else '<div class="badge">ACCOUNT PREFLIGHT FAILED</div>'
        btn=f'<form method="post" action="/?t={urllib.parse.quote(t)}"><button>⚡ CONFIRM BUY {stake:.2f} USDT</button></form>' if can else '<div class="warn bad">Account preflight failed — no order will be sent.</div>'
        body=f'''{status}<h1>{html.escape(sig['pair'])}</h1><div class="row"><span class="label">Spend</span><span class="val">{stake:.2f} USDT</span></div><div class="row"><span class="label">Order</span><span class="val">MARKET BUY</span></div><div class="row"><span class="label">TP distance</span><span class="val green">+{(tp/entry-1)*100:.2f}%</span></div><div class="row"><span class="label">SL distance</span><span class="val red">-{(1-sl/entry)*100:.2f}%</span></div><div class="warn">فتح الصفحة لا يشتري. التنفيذ الحقيقي يحصل فقط عند الضغط على CONFIRM BUY، وبعد الـfill يتحط TP/SL تلقائيًا.</div>{btn}'''
        return self.send_html(200,page('Confirm buy',body))
    def do_POST(self):
        u=urllib.parse.urlparse(self.path); t=urllib.parse.parse_qs(u.query).get('t',[''])[0]; sig=decode_token(t)
        if not sig: return self.send_html(400,page('Invalid','<h1 class="bad">Signal invalid or expired</h1>'))
        try:
            symbol=sig['pair'].replace('/',''); stake=min(float(sig['stake_usdt']),MAX_QUOTE); entry=float(sig['entry']); ref_tp=float(sig['tp']); ref_sl=float(sig['sl'])
            step,minqty,tick,minnot=market_info(symbol)
            if stake<max(5,minnot): raise RuntimeError(f'amount below Binance minimum {minnot}')
            ident=''.join(c for c in str(sig['id']) if c.isalnum())[:20]
            buy=relay('POST','/api/v3/order',{'symbol':symbol,'side':'BUY','type':'MARKET','quoteOrderQty':f'{stake:.2f}','newClientOrderId':f'tstb_{ident}','newOrderRespType':'FULL'})
            qty0=float(buy.get('executedQty') or 0); spent=float(buy.get('cummulativeQuoteQty') or buy.get('cumulativeQuoteQty') or 0)
            if not(qty0>0 and spent>0): raise RuntimeError('BUY returned zero fill')
            avg=spent/qty0; base=sig['pair'].split('/')[0]; comm=sum(float(f.get('commission') or 0) for f in buy.get('fills') or [] if f.get('commissionAsset')==base)
            qty=floor_step(max(0,qty0-comm),step)
            if float(qty)<minqty: raise RuntimeError('protected quantity below minQty')
            tp_pct=max(.002,ref_tp/entry-1); sl_pct=max(.002,1-ref_sl/entry)
            tp=tick_round(avg*(1+tp_pct),tick,'up'); st=tick_round(avg*(1-sl_pct),tick,'down'); sl=tick_round(avg*(1-sl_pct)*.9985,tick,'down')
            prot_err=None
            try:
                relay('POST','/api/v3/orderList/oco',{'symbol':symbol,'side':'SELL','quantity':qty,'listClientOrderId':f'tsto_{ident}','aboveType':'LIMIT_MAKER','abovePrice':tp,'belowType':'STOP_LOSS_LIMIT','belowStopPrice':st,'belowPrice':sl,'belowTimeInForce':'GTC'})
            except Exception as e: prot_err=str(e)
            if prot_err:
                tg(f'🚨 PROTECTION FAILED — {sig["pair"]}\nBUY executed: {spent:.4f} USDT @ {avg:.8g}\nTP/SL NOT placed.\n{prot_err[:300]}')
                body=f'<div class="badge">BINANCE SPOT</div><h1 class="bad">🚨 BUY EXECUTED — PROTECTION FAILED</h1><div class="warn">{html.escape(prot_err[:500])}</div>'
            else:
                tg(f'✅ BUY EXECUTED + PROTECTED — {sig["pair"]}\nSpent: {spent:.4f} USDT\nAvg fill: {avg:.8g}\nTP: {tp}\nSL trigger: {st}\nSL limit: {sl}')
                body=f'<div class="badge">BINANCE SPOT</div><h1 class="ok">✅ BUY EXECUTED + PROTECTED</h1><div class="row"><span class="label">Spent</span><span class="val">{spent:.4f} USDT</span></div><div class="row"><span class="label">Avg fill</span><span class="val">{avg:.8g}</span></div><div class="row"><span class="label">TP</span><span class="val green">{tp}</span></div><div class="row"><span class="label">SL Trigger</span><span class="val red">{st}</span></div><div class="row"><span class="label">SL Limit</span><span class="val red">{sl}</span></div>'
            return self.send_html(200,page('Execution result',body))
        except Exception as e:
            tg('🚨 EXECUTOR ERROR\n'+str(e)[:500]); return self.send_html(503,page('Executor error',f'<h1 class="bad">لم يتم إكمال العملية</h1><div class="warn">{html.escape(str(e)[:800])}</div>'))
    def log_message(self,*_): pass

if __name__=='__main__': ThreadingHTTPServer(('0.0.0.0',PORT),H).serve_forever()
