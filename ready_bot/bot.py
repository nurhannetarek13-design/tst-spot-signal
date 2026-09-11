#!/usr/bin/env python3
import json, os, urllib.request, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / 'config.json').read_text())
STATE_PATH = ROOT / 'state.json'


def now_iso(): return datetime.now(timezone.utc).isoformat()

def load_state():
    if STATE_PATH.exists(): return json.loads(STATE_PATH.read_text())
    return {'cash_usdt':CFG['starting_cash_usdt'],'positions':{},'closed_trades':[],'day':datetime.now(timezone.utc).date().isoformat(),'day_pnl':0.0,'last_run':None}

def save_state(s): STATE_PATH.write_text(json.dumps(s, indent=2, sort_keys=True))

def get_json(url):
    req=urllib.request.Request(url,headers={'User-Agent':'tst-ready-paper-bot/2.0'})
    with urllib.request.urlopen(req,timeout=20) as r: return json.loads(r.read().decode())

def klines(symbol, interval, limit):
    qs=f'/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}'
    bases=['https://data-api.binance.vision','https://api.binance.com','https://api1.binance.com','https://api2.binance.com','https://api3.binance.com']
    last_err=None; now_ms=int(time.time()*1000)
    for base in bases:
        try:
            rows=get_json(base+qs); out=[]
            for r in rows:
                if int(r[6]) >= now_ms: continue  # closed candles only
                out.append({'t':int(r[0]),'o':float(r[1]),'h':float(r[2]),'l':float(r[3]),'c':float(r[4]),'v':float(r[5])})
            if len(out)>=220: return out
        except Exception as e: last_err=e
    raise RuntimeError(f'All Binance market-data endpoints failed for {symbol} {interval}: {last_err}')

def ema_series(xs,n):
    if len(xs)<n: return []
    a=2/(n+1); e=sum(xs[:n])/n; out=[None]*(n-1)+[e]
    for x in xs[n:]: e=a*x+(1-a)*e; out.append(e)
    return out

def tr_series(rows):
    out=[rows[0]['h']-rows[0]['l']]
    for i in range(1,len(rows)):
        h,l,pc=rows[i]['h'],rows[i]['l'],rows[i-1]['c']
        out.append(max(h-l,abs(h-pc),abs(l-pc)))
    return out

def sma_last(xs,n): return sum(xs[-n:])/n if len(xs)>=n else None

def atr_last(rows,n): return sma_last(tr_series(rows),n)

def donor_signal(rows4h, rows1h):
    ef=CFG['ema_fast']; es=CFG['ema_slow']; ep=CFG['ema_pullback']
    c4=[x['c'] for x in rows4h]; c1=[x['c'] for x in rows1h]
    e4f=ema_series(c4,ef); e4s=ema_series(c4,es); e1f=ema_series(c1,ef); e1s=ema_series(c1,es); e1p=ema_series(c1,ep)
    if not e4f or not e4s or not e1f or not e1s or not e1p: return None
    if e4f[-1] <= e4s[-1]: return None
    if len(e4f)<4 or e4f[-4] is None or e4f[-1]-e4f[-4] <= 0: return None
    price=rows1h[-1]['c']
    if e1f[-1] <= e1s[-1] or price <= e1f[-1]: return None
    atr_n=CFG['atr_period']; atr_now=atr_last(rows1h,atr_n)
    trs=tr_series(rows1h)
    atr_hist=[]
    for i in range(atr_n-1,len(trs)): atr_hist.append(sum(trs[i-atr_n+1:i+1])/atr_n)
    if atr_now is None or len(atr_hist)<CFG['atr_ma_period']: return None
    atr_ma=sum(atr_hist[-CFG['atr_ma_period']:])/CFG['atr_ma_period']
    vol_ma=sum(x['v'] for x in rows1h[-CFG['volume_ma_period']-1:-1])/CFG['volume_ma_period']
    if atr_now <= atr_ma or rows1h[-1]['v'] <= vol_ma: return None
    lb=CFG['breakout_lookback']; prev_high=max(x['h'] for x in rows1h[-lb-1:-1])
    setup=None
    if price > prev_high:
        setup='BREAKOUT'
    else:
        prox=CFG['pullback_proximity_atr']*atr_now
        near20=abs(price-e1p[-1])<=prox; near50=abs(price-e1f[-1])<=prox
        if (near20 or near50) and price > rows1h[-2]['h']:
            setup='PULLBACK_EMA20' if near20 else 'PULLBACK_EMA50'
    if not setup: return None
    return {'setup':setup,'price':price,'atr':atr_now,'ema50_1h':e1f[-1],'ema200_1h':e1s[-1],'ema50_4h':e4f[-1],'ema200_4h':e4s[-1]}

def notify(text):
    token=os.getenv('TELEGRAM_BOT_TOKEN'); chat=os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat: print(text); return
    data=json.dumps({'chat_id':chat,'text':text}).encode(); req=urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage',data=data,headers={'Content-Type':'application/json'})
    try: urllib.request.urlopen(req,timeout=15).read()
    except Exception as e: print('telegram_error',e)

def reset_day(s):
    today=datetime.now(timezone.utc).date().isoformat()
    if s.get('day')!=today: s['day']=today; s['day_pnl']=0.0

def sell_qty(s,symbol,market_price,qty,reason):
    p=s['positions'][symbol]; qty=min(qty,p['qty']); frac=qty/p['qty']
    fill=market_price*(1-CFG['slippage_pct']/100); gross=qty*fill; fee=gross*CFG['fee_rate']; net=gross-fee
    alloc_cost=p['remaining_cost']*frac; pnl=net-alloc_cost
    p['qty']-=qty; p['remaining_cost']-=alloc_cost; s['cash_usdt']+=net; s['day_pnl']+=pnl
    s['closed_trades'].append({'symbol':symbol,'entry':p['entry'],'exit':fill,'qty':qty,'pnl':pnl,'reason':reason,'closed_at':now_iso()})
    if p['qty']<=1e-12: s['positions'].pop(symbol,None)
    notify(f"PAPER SELL {symbol} | {reason} | PnL {pnl:.4f} USDT | cash {s['cash_usdt']:.2f}")

def open_position(s,symbol,signal):
    size=min(CFG['trade_size_usdt'],s['cash_usdt']/(1+CFG['fee_rate']))
    if size<=0: return
    fill=signal['price']*(1+CFG['slippage_pct']/100); fee=size*CFG['fee_rate']; total=size+fee
    if total>s['cash_usdt']: return
    qty=size/fill; risk=CFG['stop_atr_multiplier']*signal['atr']; stop=fill-risk; r=fill-stop
    s['cash_usdt']-=total
    s['positions'][symbol]={'entry':fill,'qty':qty,'initial_qty':qty,'remaining_cost':total,'stop':stop,'tp1':fill+CFG['partial_take_at_r']*r,'partial_taken':False,'atr_entry':signal['atr'],'setup':signal['setup'],'opened_at':now_iso()}
    notify(f"PAPER BUY {symbol} | {signal['setup']} | {size:.2f} USDT | entry {fill:.6f} | stop {stop:.6f} | TP1 {fill+CFG['partial_take_at_r']*r:.6f}")

def manage_position(s,symbol,price,atr_now):
    p=s['positions'][symbol]
    if price<=p['stop']: sell_qty(s,symbol,price,p['qty'],'STOP'); return
    if not p['partial_taken'] and price>=p['tp1']:
        q=p['qty']*CFG['partial_take_pct']; sell_qty(s,symbol,price,q,'TP1_1R')
        if symbol not in s['positions']: return
        p=s['positions'][symbol]; p['partial_taken']=True; p['stop']=max(p['stop'],p['entry'])
    if symbol in s['positions'] and s['positions'][symbol]['partial_taken'] and atr_now:
        p=s['positions'][symbol]; p['stop']=max(p['stop'],price-CFG['trailing_atr_multiplier']*atr_now)

def main():
    if CFG.get('mode')!='paper': raise SystemExit('Refusing to run: ready_bot remains paper-only.')
    s=load_state(); reset_day(s); market={}
    for symbol in CFG['symbols']:
        r4=klines(symbol,CFG['trend_interval'],CFG['lookback']); r1=klines(symbol,CFG['execution_interval'],CFG['lookback'])
        market[symbol]=(r4,r1,r1[-1]['c'],atr_last(r1,CFG['atr_period']))
    for symbol in list(s['positions']):
        _,_,price,a=market[symbol]; manage_position(s,symbol,price,a)
    if s['day_pnl']<=-abs(CFG['max_daily_loss_usdt']):
        s['last_run']=now_iso(); save_state(s); notify(f"PAPER HALT | daily loss gate {s['day_pnl']:.4f} USDT"); return
    slots=CFG['max_open_positions']-len(s['positions'])
    candidates=[]
    for symbol,(r4,r1,_,_) in market.items():
        if symbol in s['positions']: continue
        sig=donor_signal(r4,r1)
        if sig: candidates.append((symbol,sig))
    for symbol,sig in candidates[:max(0,slots)]:
        open_position(s,symbol,sig)
    s['last_run']=now_iso(); save_state(s)
    print(json.dumps({'strategy':CFG['strategy_name'],'cash':s['cash_usdt'],'day_pnl':s['day_pnl'],'positions':s['positions'],'signals':[x[0] for x in candidates],'last_run':s['last_run']},indent=2))

if __name__=='__main__': main()
