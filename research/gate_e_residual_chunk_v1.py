#!/usr/bin/env python3
"""Gate E residual-dislocation event sampler. RESEARCH ONLY.

Frozen, economically motivated events based on an alt's BTC-beta residual return
plus spot/futures flow state. Emits raw gross 24/48/72h outcomes; no ML, sizing,
fees, threshold search, or live execution.
"""
from __future__ import annotations
import argparse,json,pathlib
from datetime import datetime,timedelta,timezone
import numpy as np,pandas as pd
from research.binance_vision_historical_feature_layer import build,load,DATASETS,process_kline,AUTHORIZATION

AUTH=AUTHORIZATION
HOURS=(24,48,72); BARS={h:h*4 for h in HOURS}
EVENTS=('residual_crash_exhaustion','residual_oi_flush','residual_flow_reversal')

def z(x,w=96):
    m=x.rolling(w,min_periods=w).mean(); s=x.rolling(w,min_periods=w).std()
    return (x-m)/s.replace(0,np.nan)

def btc_close(start,end):
    raw,bad=load(DATASETS['markPriceKlines'],'BTCUSDT',start,end,True)
    if raw.empty: raise RuntimeError(f'BTC benchmark unavailable: {bad[:2]}')
    b=process_kline(raw,'btc','15min').reset_index()[['open_time','btc_close']]
    return b.rename(columns={'open_time':'ts'})

def prepare(alt,btc):
    d=alt.sort_values('ts').copy(); b=btc.copy()
    d['ts']=pd.to_datetime(d.ts,utc=True); b['ts']=pd.to_datetime(b.ts,utc=True)
    d=d.merge(b,on='ts',how='left')
    for c in ['trade_close','btc_close','flow_imbalance_quote','realized_range_bps','fut_oi_change_1bar','fut_sum_taker_long_short_vol_ratio','mark_index_basis_bps']:
        d[c]=pd.to_numeric(d[c],errors='coerce')
    ar=d.trade_close.pct_change(4); br=d.btc_close.pct_change(4)
    # rolling beta uses only prior completed observations
    cov=ar.shift(1).rolling(192,min_periods=192).cov(br.shift(1)); var=br.shift(1).rolling(192,min_periods=192).var()
    beta=(cov/var.replace(0,np.nan)).clip(-1,4)
    d['res_1h']=ar-beta*br
    d['res_4h']=d.trade_close.pct_change(16)-beta*d.btc_close.pct_change(16)
    d['res_z']=z(d.res_1h); d['res4_z']=z(d.res_4h)
    d['flow_z']=z(d.flow_imbalance_quote); d['range_z']=z(d.realized_range_bps)
    d['oi_z']=z(d.fut_oi_change_1bar); d['basis_z']=z(d.mark_index_basis_bps)
    d['taker_z']=z(d.fut_sum_taker_long_short_vol_ratio)
    # Frozen before outcomes. All are long-side events.
    d['residual_crash_exhaustion']=(d.res_z<=-2.5)&(d.range_z>=1.5)&(d.flow_z<=-1.0)
    d['residual_oi_flush']=(d.res_z<=-2.0)&(d.oi_z<=-1.5)&(d.basis_z<=-1.0)
    d['residual_flow_reversal']=(d.res_z<=-2.0)&(d.flow_z>=1.0)&(d.taker_z>=0.5)
    return d

def sample(d,mask,h,core_start,core_end):
    n=BARS[h]; out=[]; idx=np.flatnonzero(mask.fillna(False).to_numpy())
    for i in idx:
        ts=pd.Timestamp(d.ts.iloc[i]); day=ts.date()
        if not(core_start<=day<=core_end) or i+n>=len(d): continue
        e=float(d.trade_close.iloc[i]); seg=d.iloc[i+1:i+n+1]
        out.append({'ts':ts.isoformat(),'ret':float(d.trade_close.iloc[i+n]/e-1),'mfe':float(seg.trade_high.max()/e-1),'mae':float(max(0,1-seg.trade_low.min()/e))})
    return out

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--symbol',required=True); ap.add_argument('--offset-days',type=int,required=True); ap.add_argument('--core-days',type=int,default=30); ap.add_argument('--out',required=True); a=ap.parse_args()
    latest=(datetime.now(timezone.utc)-timedelta(days=2)).date(); core_end=latest-timedelta(days=a.offset_days); core_start=core_end-timedelta(days=a.core_days-1)
    load_start=core_start-timedelta(days=5); load_end=min(latest,core_end+timedelta(days=3))
    alt,_=build(a.symbol,load_start,load_end,'15min',True); btc=btc_close(load_start,load_end); d=prepare(alt,btc)
    payload={'engine':'GATE_E_RESIDUAL_CHUNK_V1','authorization':AUTH,'liveTrading':False,'usesML':False,'usesSizing':False,'thresholdsFrozen':True,'symbol':a.symbol,'offsetDays':a.offset_days,'coreStart':core_start.isoformat(),'coreEnd':core_end.isoformat(),'samples':{}}
    for ev in EVENTS: payload['samples'][ev]={str(h):sample(d,d[ev],h,core_start,core_end) for h in HOURS}
    p=pathlib.Path(a.out); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(payload))
    print(json.dumps({'symbol':a.symbol,'offset':a.offset_days,'samples':sum(len(v) for e in payload['samples'].values() for v in e.values())}))
if __name__=='__main__': main()
