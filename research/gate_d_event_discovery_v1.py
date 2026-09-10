#!/usr/bin/env python3
"""Gate D: unoptimized event discovery. RESEARCH ONLY.

Tests frozen event definitions on completed 15m bars. No ML, no sizing, no entry
optimization. Gate is based on raw conditional forward returns at 24/48/72h.
Inputs are lagged/rolling using only information available at event timestamp.
"""
from __future__ import annotations
import argparse,json,pathlib
from datetime import datetime,timedelta,timezone
import numpy as np,pandas as pd
from research.binance_vision_historical_feature_layer import build

AUTH='RESEARCH_ONLY'
HOURS=(24,48,72)
BARS={h:h*4 for h in HOURS}

def z(x,w=96):
    m=x.rolling(w,min_periods=w).mean();s=x.rolling(w,min_periods=w).std()
    return (x-m)/s.replace(0,np.nan)

def prepare(d):
    d=d.sort_values('ts').copy()
    cols=['trade_close','flow_imbalance_quote','agg_trade_count','realized_range_bps','fut_oi_change_1bar','fut_sum_taker_long_short_vol_ratio','mark_index_basis_bps']
    for c in cols:
        if c in d:d[c]=pd.to_numeric(d[c],errors='coerce')
    d['ret_1h']=d.trade_close.pct_change(4)
    d['ret_4h']=d.trade_close.pct_change(16)
    d['ret_z']=z(d.ret_1h);d['flow_z']=z(d.flow_imbalance_quote);d['vol_z']=z(d.agg_trade_count)
    d['range_z']=z(d.realized_range_bps);d['oi_z']=z(d.fut_oi_change_1bar)
    d['basis_z']=z(d.mark_index_basis_bps);d['taker_z']=z(d.fut_sum_taker_long_short_vol_ratio)
    # Definitions frozen before outcome calculation. Long-side exhaustion/dislocation events.
    d['liquidity_crash_exhaustion']=(d.ret_z<=-2.5)&(d.vol_z>=2.0)&(d.range_z>=1.5)&(d.flow_z<=-1.5)
    d['oi_dislocation_reversal']=(d.ret_z<=-2.0)&(d.oi_z<=-1.5)&(d.flow_z<=-1.0)
    d['basis_flow_dislocation']=(d.basis_z<=-2.0)&(d.flow_z>=1.0)&(d.ret_4h<0)
    d['vol_expansion_exhaustion']=(d.range_z>=2.0)&(d.vol_z>=2.0)&(d.ret_z<=-2.0)&(d.flow_z>-2.5)
    return d

def event_stats(d,mask,h):
    n=BARS[h];f=d.trade_close.shift(-n)/d.trade_close-1
    # non-overlap: keep first event then suppress same-event horizon to reduce pseudo-replication
    idx=np.flatnonzero(mask.fillna(False).to_numpy());keep=[];last=-10**9
    for i in idx:
        if i-last>=n:keep.append(i);last=i
    r=f.iloc[keep].dropna().to_numpy(float)
    if not len(r):return {'n':0,'meanPct':None,'medianPct':None,'hitRate':None,'medianMFEpct':None,'medianMAEpct':None,'mfeMaeRatio':None}
    mfe=[];mae=[]
    for i in keep:
        if i+n>=len(d):continue
        e=float(d.trade_close.iloc[i]);seg=d.iloc[i+1:i+n+1]
        mfe.append(float(seg.trade_high.max()/e-1));mae.append(float(max(0,1-seg.trade_low.min()/e)))
    mmfe=float(np.median(mfe)) if mfe else np.nan;mmae=float(np.median(mae)) if mae else np.nan
    return {'n':int(len(r)),'meanPct':float(r.mean()*100),'medianPct':float(np.median(r)*100),'hitRate':float((r>0).mean()),'medianMFEpct':mmfe*100,'medianMAEpct':mmae*100,'mfeMaeRatio':float(mmfe/mmae) if mmae>0 else None}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--days',type=int,default=90);ap.add_argument('--out',default='validation/edges/gate-d-event-discovery-v1.json');a=ap.parse_args()
    end=(datetime.now(timezone.utc)-timedelta(days=2)).date();start=end-timedelta(days=a.days-1);frames=[]
    for s in ['BTCUSDT','ETHUSDT','SOLUSDT']:
        d,_=build(s,start,end,'15min',True);d['symbol']=s;frames.append(prepare(d))
    names=['liquidity_crash_exhaustion','oi_dislocation_reversal','basis_flow_dislocation','vol_expansion_exhaustion'];results={}
    for name in names:
        per={}
        pooled=[]
        for d in frames:
            sym=str(d.symbol.iloc[0]);per[sym]={str(h):event_stats(d,d[name],h) for h in HOURS}
            x=d.copy();x['_event']=x[name];pooled.append(x)
        # Aggregate by concatenating symbol-specific event forward returns is intentionally avoided here;
        # require cross-symbol evidence instead of letting one symbol dominate.
        gates=[]
        for h in HOURS:
            eligible=[per[s][str(h)] for s in per if per[s][str(h)]['n']>=3]
            pass_h=bool(len(eligible)>=2 and all(x['meanPct']>=1.5 and x['medianPct']>0 and x['hitRate']>.55 and (x['mfeMaeRatio'] or 0)>=2 for x in eligible))
            gates.append(pass_h)
        results[name]={'symbols':per,'rawGatePass':any(gates),'passingHorizons':[h for h,g in zip(HOURS,gates) if g]}
    winners=[k for k,v in results.items() if v['rawGatePass']]
    out={'engine':'GATE_D_EVENT_DISCOVERY_V1','authorization':AUTH,'liveTrading':False,'usesML':False,'usesSizing':False,'thresholdsFrozen':True,'period':{'start':start.isoformat(),'end':end.isoformat()},'horizonsHours':list(HOURS),'rawGate':{'meanGrossPctMin':1.5,'medianPositive':True,'hitRateMinExclusive':.55,'medianMFEtoMAEMin':2.0,'crossSymbolEligibleMin':2,'eventsPerSymbolHorizonMin':3},'events':results,'winners':winners,'next':'FREEZE_EVENT_AND_OOS' if winners else 'REJECT_GATE_D_V1'}
    p=pathlib.Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(out,indent=2));print(json.dumps({'winners':winners,'next':out['next']}))
if __name__=='__main__':main()
