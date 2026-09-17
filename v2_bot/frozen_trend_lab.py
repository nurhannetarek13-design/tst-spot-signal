"""Pre-registered, read-only Binance Spot 4h trend comparison.

Research only: no credentials, order interface, strategy registration or auto-promotion.
Never infer a deployable strategy from passing one retrospective dataset.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

BASE = 'https://data-api.binance.vision/api/v3/klines'
BAR_MS = 4 * 60 * 60 * 1000
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
BEGIN = '2020-10-01'
WINDOWS = (('discovery','2021-01-01','2024-01-01'),
           ('validation','2024-01-01','2025-01-01'),
           ('untouched_oos','2025-01-01', None))
RULES = ('SMA200_STATE', 'DONCHIAN55_SMA200', 'DUAL_EMA50_200')
FEE = 0.001
QUOTE = 10.0


def ms(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()*1000)


def fetch(symbol, *, client, now_ms):
    cursor=ms(BEGIN)
    rows=[]
    while cursor < now_ms:
        res=client.get(BASE,params={'symbol':symbol,'interval':'4h','limit':1000,'startTime':cursor,'endTime':now_ms})
        res.raise_for_status()
        payload=res.json()
        if not isinstance(payload,list):
            raise ValueError('invalid_kline_payload')
        if not payload:
            break
        for bar in payload:
            if int(bar[6]) < now_ms:
                rows.append({'t':int(bar[0]),'o':float(bar[1]),'h':float(bar[2]),
                             'l':float(bar[3]),'c':float(bar[4])})
        nxt=int(payload[-1][0])+BAR_MS
        if nxt <= cursor:
            raise ValueError('pagination_not_advancing')
        cursor=nxt
        if len(payload)<1000:
            break
        time.sleep(0.02)
    if len(rows)<2500:
        raise ValueError(f'insufficient_4h_history:{symbol}:{len(rows)}')
    for a,b in zip(rows,rows[1:]):
        if b['t']-a['t'] != BAR_MS:
            raise ValueError(f'historical_candle_gap:{symbol}:{a["t"]}:{b["t"]}')
    return rows


def indicators(bars):
    n=len(bars)
    sma=[None]*n; fast=[None]*n; slow=[None]*n; atr=[None]*n
    prices=[r['c'] for r in bars]
    sm=sum(prices[:200]); sma[199]=sm/200
    for i in range(200,n):
        sm+=prices[i]-prices[i-200]; sma[i]=sm/200
    for period,out in ((50,fast),(200,slow)):
        out[period-1]=sum(prices[:period])/period
        for i in range(period,n):
            out[i]=out[i-1]+(2/(period+1))*(prices[i]-out[i-1])
    trs=[]
    for i,b in enumerate(bars):
        prior=prices[i-1] if i else b['o']
        trs.append(max(b['h']-b['l'],abs(b['h']-prior),abs(b['l']-prior)))
    atr[13]=sum(trs[:14])/14
    for i in range(14,n):
        atr[i]=(13*atr[i-1]+trs[i])/14
    return sma,fast,slow,atr


def signal(rule,bars,ind,i):
    sma,fast,slow,_=ind
    if i<201:
        return False,False
    c=bars[i]['c']
    if rule=='SMA200_STATE':
        return c>sma[i],c<sma[i]
    if rule=='DONCHIAN55_SMA200':
        # Strictly previous completed bars; never include signal bar in channel.
        hi=max(b['h'] for b in bars[i-55:i])
        lo=min(b['l'] for b in bars[i-20:i])
        return c>hi and c>sma[i],c<lo or c<sma[i]
    if rule=='DUAL_EMA50_200':
        return fast[i]>slow[i] and c>slow[i],fast[i]<slow[i] or c<slow[i]
    raise ValueError(rule)


def simulate(bars,ind,rule,start_ms,end_ms,slippage_bps):
    assert rule in RULES and 0 <= slippage_bps <= 100
    slip=slippage_bps/10000
    trades=[]; pos=None; pending=None; entry_index=None; stop=None
    daily={}
    indices=[i for i,b in enumerate(bars) if start_ms<=b['t']<end_ms]
    if not indices or indices[0]<201:
        raise ValueError('window_has_no_warmup')

    def close(price,i,reason):
        nonlocal pos,stop,entry_index
        ex=price*(1-slip)
        pnl=pos['qty']*(ex-pos['entry']) - FEE*(pos['qty']*pos['entry'] + pos['qty']*ex)
        day=datetime.fromtimestamp(bars[i]['t']/1000,timezone.utc).date().isoformat()
        daily[day]=daily.get(day,0)+pnl
        trades.append({'signal_t':pos['signal_t'],'entry_t':bars[entry_index]['t'],
                       'exit_t':bars[i]['t'],'entry':pos['entry'],'exit':ex,
                       'pnl':round(pnl,8),'reason':reason})
        pos=None; stop=None; entry_index=None

    for i in indices:
        b=bars[i]
        day=datetime.fromtimestamp(b['t']/1000,timezone.utc).date().isoformat()
        if pending=='SELL' and pos:
            close(b['o'],i,'next_open_exit')
        elif pending=='BUY' and pos is None and daily.get(day,0)>-2:
            entry=b['o']*(1+slip)
            dist=3*ind[3][i-1]
            if entry>dist>0:
                pos={'entry':entry,'qty':QUOTE/entry,'signal_t':bars[i-1]['t']}
                entry_index=i; stop=entry-dist
        pending=None
        if pos is not None:
            if b['o']<=stop:
                close(b['o'],i,'stop_gap_open')
            elif b['l']<=stop:
                close(stop,i,'stop_first_intrabar')
            else:
                _,exit_signal=signal(rule,bars,ind,i)
                if exit_signal:
                    pending='SELL'
                # Update trailing stop only after completed candle, not at its high.
                stop=max(stop,b['c']-3*ind[3][i])
        elif daily.get(day,0)>-2 and i+1<len(bars) and bars[i+1]['t']<end_ms:
            entry_signal,_=signal(rule,bars,ind,i)
            if entry_signal:
                pending='BUY'
    if pos is not None:
        close(bars[indices[-1]]['c'],indices[-1],'boundary_mark_to_market')
    pnls=[t['pnl'] for t in trades]
    gp=sum(p for p in pnls if p>0); gl=-sum(p for p in pnls if p<0)
    eq=peak=dd=0.0
    for pnl in pnls:
        eq+=pnl;peak=max(peak,eq);dd=max(dd,peak-eq)
    return {'closed':len(trades),'wins':sum(p>0 for p in pnls),
            'pf':round(gp/gl,4) if gl>0 else None,
            'net_usdt':round(eq,6),'expectancy_usdt':round(eq/len(trades),6) if trades else None,
            'realized_max_dd_usdt':round(dd,6),
            'boundary_marks':sum(t['reason']=='boundary_mark_to_market' for t in trades),
            'stop_gaps':sum(t['reason']=='stop_gap_open' for t in trades),
            'trades':trades}


def run():
    now_ms=int(time.time()*1000)
    report={'status':'RESEARCH_ONLY_NO_AUTO_PROMOTION','live_trading':False,'trade_size_usdt':QUOTE,
            'symbols':list(SYMBOLS),'rules':list(RULES),
            'frozen_windows':WINDOWS,
            'costs':{'fee_each_side':FEE,'base_slippage_bps_each_side':5,'stress_slippage_bps_each_side':15},
            'model_limitations':['4h OHLCV only; no historical L1 spread or intrabar sequence',
                                 'independent single-symbol simulations; not an executable multi-asset portfolio',
                                 'realized-trade drawdown omits intra-position mark-to-market',
                                 'boundary marks at last completed close are not guaranteed fills',
                                 'selected current symbols induce survivorship bias'],
            'results':{}}
    with httpx.Client(timeout=30,headers={'User-Agent':'tst-frozen-trend-lab/1.0'}) as client:
        for symbol in SYMBOLS:
            bars=fetch(symbol,client=client,now_ms=now_ms)
            ind=indicators(bars)
            asset={'bars':len(bars),'first_t':bars[0]['t'],'last_t':bars[-1]['t'],'rules':{}}
            for rule in RULES:
                windows={}
                for name,begin,end in WINDOWS:
                    a=ms(begin);z=ms(end) if end else now_ms
                    if not (bars[0]['t'] < a < bars[-1]['t']):
                        raise ValueError(f'not_enough_data:{symbol}:{name}')
                    base=simulate(bars,ind,rule,a,z,5)
                    stress=simulate(bars,ind,rule,a,z,15)
                    # Exact same signal/exit rules, only two frozen cost assumptions vary.
                    windows[name]={'base':{k:v for k,v in base.items() if k!='trades'},
                                   'stress':{k:v for k,v in stress.items() if k!='trades'},
                                   'trades':base['trades']}
                asset['rules'][rule]=windows
            report['results'][symbol]=asset
    # Strict gate is descriptive, never a live/paper deployment trigger.
    decisions={}
    for rule in RULES:
        blockers=[]
        for symbol in SYMBOLS:
            for window in ('validation','untouched_oos'):
                stats=report['results'][symbol]['rules'][rule][window]['stress']
                if stats['closed']<8 or stats['pf'] is None or stats['pf']<1.2 or stats['net_usdt']<=0:
                    blockers.append(f'{symbol}:{window}:insufficient_or_negative_stress_evidence')
        decisions[rule]={'research_gate_passed':not blockers,'blockers':blockers,'live_approved':False}
    report['frozen_research_gates']=decisions
    Path('frozen_trend_lab_report.json').write_text(json.dumps(report,indent=2,sort_keys=True))
    print(json.dumps({'status':report['status'],'symbols':SYMBOLS,
                      'summary':{s:{r:{w:{c:report['results'][s]['rules'][r][w][c] for c in ('base','stress')}
                                      for w in ('discovery','validation','untouched_oos')}
                                    for r in RULES} for s in SYMBOLS},
                      'gates':decisions,'live_trading':False},sort_keys=True))

if __name__=='__main__':
    run()
