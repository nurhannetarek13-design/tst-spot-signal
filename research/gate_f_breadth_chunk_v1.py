#!/usr/bin/env python3
"""Gate F: market breadth / lead-lag raw event sampler. RESEARCH ONLY.

Frozen global events from BTC 24h return plus alt breadth. Emits symbol-level raw
48/72/168h outcomes. No ML, sizing, fees, or threshold tuning.
"""
from __future__ import annotations
import argparse,json,pathlib
from datetime import datetime,timedelta,timezone
import numpy as np,pandas as pd
from research.binance_vision_historical_feature_layer import build,AUTHORIZATION

AUTH=AUTHORIZATION
SYMS=('ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ADAUSDT')
DISC=('ETHUSDT','SOLUSDT','BNBUSDT'); HOLD=('XRPUSDT','DOGEUSDT','ADAUSDT')
HOURS=(48,72,168); BARS={h:h*4 for h in HOURS}
EVENTS=('btc_leads_breadth_lag','broad_risk_on_continuation','breadth_reacceleration')

def prep(d):
    x=d.sort_values('ts').copy(); x['ts']=pd.to_datetime(x.ts,utc=True)
    for c in ['trade_close','trade_high','trade_low']: x[c]=pd.to_numeric(x[c],errors='coerce')
    return x[['ts','trade_close','trade_high','trade_low']]

def sample_one(d,mask,h,core_start,core_end):
    n=BARS[h]; out=[]; idx=np.flatnonzero(mask.fillna(False).to_numpy())
    for i in idx:
        ts=pd.Timestamp(d.ts.iloc[i]); day=ts.date()
        if not(core_start<=day<=core_end) or i+n>=len(d): continue
        e=float(d.trade_close.iloc[i]); seg=d.iloc[i+1:i+n+1]
        out.append({'ts':ts.isoformat(),'ret':float(d.trade_close.iloc[i+n]/e-1),'mfe':float(seg.trade_high.max()/e-1),'mae':float(max(0,1-seg.trade_low.min()/e))})
    return out

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--offset-days',type=int,required=True); ap.add_argument('--core-days',type=int,default=10); ap.add_argument('--out',required=True); a=ap.parse_args()
    latest=(datetime.now(timezone.utc)-timedelta(days=2)).date(); core_end=latest-timedelta(days=a.offset_days); core_start=core_end-timedelta(days=a.core_days-1)
    load_start=core_start-timedelta(days=8); load_end=min(latest,core_end+timedelta(days=8))
    frames={}
    for s in ('BTCUSDT',)+SYMS:
        d,_=build(s,load_start,load_end,'15min',True); frames[s]=prep(d)
    base=frames['BTCUSDT'][['ts','trade_close']].rename(columns={'trade_close':'btc'})
    panel=base.copy()
    for s in SYMS:
        panel=panel.merge(frames[s][['ts','trade_close']].rename(columns={'trade_close':s}),on='ts',how='inner')
    panel=panel.sort_values('ts').reset_index(drop=True)
    panel['btc_r24']=panel.btc.pct_change(96)
    pos=[]
    for s in SYMS:
        r24=panel[s].pct_change(96); pos.append((r24>0).astype(float).rename(s))
    panel['breadth']=pd.concat(pos,axis=1).mean(axis=1)
    panel['breadth_delta_6h']=panel.breadth-panel.breadth.shift(24)
    # Frozen global long-side events.
    panel['btc_leads_breadth_lag']=(panel.btc_r24>=0.02)&(panel.breadth<=0.34)
    panel['broad_risk_on_continuation']=(panel.btc_r24>=0.015)&(panel.breadth>=0.80)
    panel['breadth_reacceleration']=(panel.btc_r24>=0)&(panel.breadth>=0.67)&(panel.breadth_delta_6h>=0.34)
    payload={'engine':'GATE_F_BREADTH_CHUNK_V1','authorization':AUTH,'liveTrading':False,'usesML':False,'usesSizing':False,'thresholdsFrozen':True,'offsetDays':a.offset_days,'coreStart':core_start.isoformat(),'coreEnd':core_end.isoformat(),'samples':{}}
    for ev in EVENTS:
        payload['samples'][ev]={}
        for s in SYMS:
            d=frames[s].merge(panel[['ts',ev]],on='ts',how='inner')
            payload['samples'][ev][s]={str(h):sample_one(d,d[ev],h,core_start,core_end) for h in HOURS}
    p=pathlib.Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(payload))
    print(json.dumps({'offset':a.offset_days,'samples':sum(len(v) for ev in payload['samples'].values() for s in ev.values() for v in s.values())}))
if __name__=='__main__':main()
