#!/usr/bin/env python3
"""Research-only shadow trade labeler.
Labels OPEN paper trades against later OHLC bars using TP/SL/timeout semantics.
Never places orders and never reads exchange credentials.
"""
from __future__ import annotations
import argparse,json
from datetime import datetime,timezone

DEFAULT_COSTS={
    'fee_roundtrip_pct':0.20,
    'spread_roundtrip_pct':0.04,
    'slippage_roundtrip_pct':0.04,
}

def _f(x,default=0.0):
    try:return float(x)
    except:return default

def total_cost_pct(costs=None):
    c=dict(DEFAULT_COSTS); c.update(costs or {})
    return c, float(c['fee_roundtrip_pct']+c['spread_roundtrip_pct']+c['slippage_roundtrip_pct'])

def label_one(trade:dict,bars:list[dict],fee_roundtrip_pct:float|None=None,timeout_min:int=240,costs:dict|None=None):
    if trade.get('status')!='OPEN':return trade
    entry=_f(trade['entry_price']); tp=entry*(1+_f(trade['tp_pct'])/100); sl=entry*(1-_f(trade['sl_pct'])/100)
    t0=datetime.fromisoformat(str(trade['ts']).replace('Z','+00:00')).timestamp()*1000
    chosen=None; mfe=-1e9; mae=1e9
    for b in bars:
        ts=int(b['ts'])
        if ts<t0:continue
        hi=_f(b['high']); lo=_f(b['low']); close=_f(b['close'])
        mfe=max(mfe,(hi/entry-1)*100); mae=min(mae,(lo/entry-1)*100)
        age=(ts-t0)/60000
        # Conservative ambiguity rule: if both touched in same bar, SL wins.
        if lo<=sl: chosen=('SL',sl,age,0); break
        if hi>=tp: chosen=('TP',tp,age,1); break
        if age>=timeout_min: chosen=('TIMEOUT',close,age,int(close>entry)); break
    if not chosen:return trade
    reason,px,hold,win=chosen; gross=(px/entry-1)*100
    if fee_roundtrip_pct is not None:
        c={'fee_roundtrip_pct':float(fee_roundtrip_pct),'spread_roundtrip_pct':0.0,'slippage_roundtrip_pct':0.0}; total=float(fee_roundtrip_pct)
    else:
        c,total=total_cost_pct(costs)
    net=gross-total
    out=dict(trade); out.update({
        'status':'CLOSED','close_reason':reason,'exit_price':round(px,12),'tp_before_sl':win,
        'gross_return_pct':round(gross,6),'fee_roundtrip_pct':round(c['fee_roundtrip_pct'],6),
        'spread_roundtrip_pct':round(c['spread_roundtrip_pct'],6),'slippage_roundtrip_pct':round(c['slippage_roundtrip_pct'],6),
        'total_cost_pct':round(total,6),'net_return_pct':round(net,6),'mfe_pct':round(mfe,6),'mae_pct':round(mae,6),'holding_min':round(hold,2)
    })
    return out

def process(trades,bars_by_symbol,fee_roundtrip_pct=None,timeout_min=240,costs=None):
    return [label_one(t,bars_by_symbol.get(t.get('symbol'),[]),fee_roundtrip_pct,timeout_min,costs) for t in trades]

def selftest():
    t={'ts':'2026-09-10T16:00:00Z','symbol':'SOLUSDT','status':'OPEN','entry_price':'100','tp_pct':'1.2','sl_pct':'0.7'}
    bars={'SOLUSDT':[{'ts':datetime(2026,9,10,16,15,tzinfo=timezone.utc).timestamp()*1000,'high':101.3,'low':99.8,'close':101.0}]}
    x=process([t],bars)[0]
    assert x['status']=='CLOSED' and x['close_reason']=='TP' and x['tp_before_sl']==1
    assert x['total_cost_pct']==0.28 and x['net_return_pct']==0.92
    print(json.dumps({'selftest':'PASS','sample':x}))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--selftest',action='store_true');a=ap.parse_args()
    if a.selftest:selftest()
if __name__=='__main__':main()
