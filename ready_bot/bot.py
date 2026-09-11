#!/usr/bin/env python3
import json, os, time, math, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / 'config.json').read_text())
STATE_PATH = ROOT / 'state.json'


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {
        'cash_usdt': CFG['starting_cash_usdt'],
        'positions': {},
        'closed_trades': [],
        'day': datetime.now(timezone.utc).date().isoformat(),
        'day_pnl': 0.0,
        'last_run': None
    }


def save_state(s):
    STATE_PATH.write_text(json.dumps(s, indent=2, sort_keys=True))


def get_json(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode())


def klines(symbol, interval, limit):
    url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}'
    rows = get_json(url)
    out=[]
    for r in rows:
        out.append({'t':r[0], 'o':float(r[1]), 'h':float(r[2]), 'l':float(r[3]), 'c':float(r[4]), 'v':float(r[5])})
    return out


def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ema(xs, n):
    if len(xs) < n: return None
    a = 2/(n+1)
    e = sum(xs[:n])/n
    for x in xs[n:]: e = a*x + (1-a)*e
    return e


def rsi(xs, n=14):
    if len(xs) <= n: return None
    gains=[]; losses=[]
    for i in range(-n,0):
        d=xs[i]-xs[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/n; al=sum(losses)/n
    if al == 0: return 100.0
    rs=ag/al
    return 100-(100/(1+rs))


def atr(rows, n=14):
    if len(rows) <= n: return None
    trs=[]
    for i in range(1,len(rows)):
        h,l,pc=rows[i]['h'],rows[i]['l'],rows[i-1]['c']
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs[-n:])/n


def strategy_score(rows):
    closes=[x['c'] for x in rows]
    price=closes[-1]
    votes=[]

    # Trend continuation: price above EMA50 and EMA50 above EMA200.
    e50=ema(closes,50); e200=ema(closes,200)
    if CFG['strategies'].get('trend') and e50 and e200:
        votes.append(('trend', 1 if price > e50 > e200 else 0))

    # Donchian-style breakout using previous 20 completed bars.
    if CFG['strategies'].get('breakout') and len(rows) >= 22:
        prev_high=max(x['h'] for x in rows[-21:-1])
        vol_avg=sum(x['v'] for x in rows[-21:-1])/20
        votes.append(('breakout', 1 if price > prev_high and rows[-1]['v'] > vol_avg*1.2 else 0))

    # Mean reversion only inside broader uptrend: RSI oversold + price above EMA200.
    rv=rsi(closes,14)
    if CFG['strategies'].get('mean_reversion') and rv is not None and e200:
        votes.append(('mean_reversion', 1 if rv < 32 and price > e200 else 0))

    score=sum(v for _,v in votes)
    return score, {k:v for k,v in votes}, {'price':price,'rsi':rv,'ema50':e50,'ema200':e200,'atr':atr(rows)}


def notify(text):
    token=os.getenv('TELEGRAM_BOT_TOKEN'); chat=os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat:
        print(text); return
    data=json.dumps({'chat_id':chat,'text':text}).encode()
    req=urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage', data=data, headers={'Content-Type':'application/json'})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print('telegram_error', e)


def reset_day(s):
    today=datetime.now(timezone.utc).date().isoformat()
    if s.get('day') != today:
        s['day']=today; s['day_pnl']=0.0


def close_position(s, symbol, price, reason):
    p=s['positions'].pop(symbol)
    qty=p['qty']
    pnl=(price-p['entry'])*qty
    s['cash_usdt'] += qty*price
    s['day_pnl'] += pnl
    trade={'symbol':symbol,'entry':p['entry'],'exit':price,'qty':qty,'pnl':pnl,'reason':reason,'closed_at':datetime.now(timezone.utc).isoformat()}
    s['closed_trades'].append(trade)
    notify(f"PAPER CLOSE {symbol} | {reason} | PnL {pnl:.4f} USDT | cash {s['cash_usdt']:.2f}")


def open_position(s, symbol, price, meta):
    size=min(CFG['trade_size_usdt'], s['cash_usdt'])
    if size <= 0: return
    qty=size/price
    tp=price*(1+CFG['take_profit_pct'])
    sl=price*(1-CFG['stop_loss_pct'])
    s['cash_usdt'] -= size
    s['positions'][symbol]={'entry':price,'qty':qty,'notional':size,'tp':tp,'sl':sl,'opened_at':datetime.now(timezone.utc).isoformat(),'meta':meta}
    notify(f"PAPER BUY {symbol} | {size:.2f} USDT | entry {price:.6f} | TP {tp:.6f} | SL {sl:.6f} | score {meta['score']}")


def main():
    if CFG.get('mode') != 'paper':
        raise SystemExit('Refusing to run: ready_bot is paper-only. Keep mode=paper.')
    s=load_state(); reset_day(s)

    market={}
    for symbol in CFG['symbols']:
        rows=klines(symbol,CFG['interval'],CFG['lookback'])
        market[symbol]=(rows, rows[-1]['c'])

    # exits first
    for symbol in list(s['positions']):
        price=market[symbol][1]
        p=s['positions'][symbol]
        if price >= p['tp']: close_position(s,symbol,price,'TAKE_PROFIT')
        elif price <= p['sl']: close_position(s,symbol,price,'STOP_LOSS')

    # hard daily loss gate
    if s['day_pnl'] <= -abs(CFG['max_daily_loss_usdt']):
        s['last_run']=datetime.now(timezone.utc).isoformat(); save_state(s)
        notify(f"PAPER HALT | daily loss gate reached: {s['day_pnl']:.4f} USDT")
        return

    slots=CFG['max_open_positions']-len(s['positions'])
    candidates=[]
    for symbol,(rows,price) in market.items():
        if symbol in s['positions']: continue
        score,votes,ind=strategy_score(rows)
        if score >= CFG['min_consensus_score']:
            candidates.append((score,symbol,price,votes,ind))

    candidates.sort(reverse=True)
    for score,symbol,price,votes,ind in candidates[:max(0,slots)]:
        if s['cash_usdt'] < CFG['trade_size_usdt']: break
        open_position(s,symbol,price,{'score':score,'votes':votes,'indicators':ind})

    s['last_run']=datetime.now(timezone.utc).isoformat()
    save_state(s)
    print(json.dumps({'cash':s['cash_usdt'],'day_pnl':s['day_pnl'],'positions':s['positions'],'last_run':s['last_run']}, indent=2))


if __name__ == '__main__':
    main()
