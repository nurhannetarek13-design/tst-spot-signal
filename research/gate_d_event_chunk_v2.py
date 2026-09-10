#!/usr/bin/env python3
"""Gate D V2 chunk sampler. RESEARCH ONLY.

Uses the exact frozen event definitions from gate_d_event_discovery_v1, but emits
raw timestamped forward-return/MFE/MAE samples for one core window so multiple
30-day jobs can be aggregated without changing thresholds.
"""
from __future__ import annotations
import argparse,json,pathlib
from datetime import datetime,timedelta,timezone
import numpy as np,pandas as pd
from research.binance_vision_historical_feature_layer import build
from research.gate_d_event_discovery_v1 import prepare,HOURS,BARS,AUTH

EVENTS=['liquidity_crash_exhaustion','oi_dislocation_reversal','basis_flow_dislocation','vol_expansion_exhaustion']

def samples(d,mask,h,core_start,core_end):
    n=BARS[h]; out=[]
    idx=np.flatnonzero(mask.fillna(False).to_numpy())
    for i in idx:
        ts=pd.Timestamp(d.ts.iloc[i]).to_pydatetime()
        if not (core_start <= ts.date() <= core_end): continue
        if i+n>=len(d): continue
        e=float(d.trade_close.iloc[i]); seg=d.iloc[i+1:i+n+1]
        ret=float(d.trade_close.iloc[i+n]/e-1)
        mfe=float(seg.trade_high.max()/e-1); mae=float(max(0,1-seg.trade_low.min()/e))
        out.append({'ts':pd.Timestamp(d.ts.iloc[i]).isoformat(),'ret':ret,'mfe':mfe,'mae':mae})
    return out

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--offset-days',type=int,required=True);ap.add_argument('--core-days',type=int,default=30);ap.add_argument('--out',required=True);a=ap.parse_args()
    latest=(datetime.now(timezone.utc)-timedelta(days=2)).date()
    core_end=latest-timedelta(days=a.offset_days)
    core_start=core_end-timedelta(days=a.core_days-1)
    # warmup for 96-bar z-scores + 72h forward buffer where data exists
    load_start=core_start-timedelta(days=2)
    load_end=min(latest,core_end+timedelta(days=3))
    payload={'engine':'GATE_D_EVENT_CHUNK_V2','authorization':AUTH,'liveTrading':False,'thresholdsFrozen':True,'offsetDays':a.offset_days,'coreStart':core_start.isoformat(),'coreEnd':core_end.isoformat(),'samples':{}}
    for s in ['BTCUSDT','ETHUSDT','SOLUSDT']:
        d,_=build(s,load_start,load_end,'15min',True); d['symbol']=s; d=prepare(d)
        payload['samples'][s]={}
        for ev in EVENTS:
            payload['samples'][s][ev]={str(h):samples(d,d[ev],h,core_start,core_end) for h in HOURS}
    p=pathlib.Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(payload));
    print(json.dumps({'offsetDays':a.offset_days,'coreStart':payload['coreStart'],'coreEnd':payload['coreEnd'],'sampleCount':sum(len(v) for s in payload['samples'].values() for e in s.values() for v in e.values())}))
if __name__=='__main__': main()
