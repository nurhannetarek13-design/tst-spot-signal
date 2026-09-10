#!/usr/bin/env python3
"""Aggregate Gate E residual-event chunks into frozen discovery/holdout gate."""
from __future__ import annotations
import argparse,glob,json,pathlib
import numpy as np,pandas as pd
from research.gate_e_residual_chunk_v1 import AUTH,HOURS,EVENTS
DISC=('ETHUSDT','SOLUSDT','BNBUSDT'); HOLD=('XRPUSDT','DOGEUSDT','ADAUSDT')

def dedup(rows,h):
    rows=sorted(rows,key=lambda x:x['ts']); out=[]; last=None
    for r in rows:
        ts=pd.Timestamp(r['ts'])
        if last is None or (ts-last).total_seconds()>=h*3600: out.append(r); last=ts
    return out

def stats(rows):
    if not rows:return {'n':0,'meanPct':None,'medianPct':None,'hitRate':None,'mfeMaeRatio':None}
    r=np.array([x['ret'] for x in rows],float); mfe=np.array([x['mfe'] for x in rows],float); mae=np.array([x['mae'] for x in rows],float)
    mmfe=float(np.median(mfe)); mmae=float(np.median(mae))
    return {'n':len(rows),'meanPct':float(r.mean()*100),'medianPct':float(np.median(r)*100),'hitRate':float((r>0).mean()),'mfeMaeRatio':float(mmfe/mmae) if mmae>0 else None}

def group_stats(by_symbol,syms,ev,h):
    pooled=[]; qualifying=0
    for s in syms:
        rows=dedup(by_symbol.get(s,{}).get(ev,{}).get(str(h),[]),h)
        if len(rows)>=3: qualifying+=1
        pooled+=rows
    st=stats(pooled); st['qualifyingSymbols']=qualifying; return st

def passes(st,min_n):
    return bool(st['n']>=min_n and st['qualifyingSymbols']>=2 and st['meanPct']>=1.5 and st['medianPct']>0 and st['hitRate']>.55 and (st['mfeMaeRatio'] or 0)>=2)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--glob',default='gate-e-chunks/*.json'); ap.add_argument('--out',default='validation/edges/gate-e-residual-v1.json'); a=ap.parse_args()
    files=glob.glob(a.glob); assert len(files)>=18, files
    chunks=[json.load(open(f)) for f in files]
    by={}
    for c in chunks:
        assert c['authorization']==AUTH and c['liveTrading'] is False and c['thresholdsFrozen'] is True
        s=c['symbol']; by.setdefault(s,{})
        for ev in EVENTS:
            by[s].setdefault(ev,{})
            for h in HOURS: by[s][ev].setdefault(str(h),[]); by[s][ev][str(h)]+=c['samples'][ev][str(h)]
    results={}; winners=[]
    for ev in EVENTS:
        results[ev]={}
        for h in HOURS:
            d=group_stats(by,DISC,ev,h); o=group_stats(by,HOLD,ev,h); ok=passes(d,20) and passes(o,15)
            results[ev][str(h)]={'discovery':d,'symbolHoldout':o,'rawGatePass':ok}
            if ok:winners.append({'event':ev,'horizonHours':h})
    starts=[c['coreStart'] for c in chunks]; ends=[c['coreEnd'] for c in chunks]
    out={'engine':'GATE_E_RESIDUAL_DISCOVERY_V1','authorization':AUTH,'liveTrading':False,'usesML':False,'usesSizing':False,'thresholdsFrozen':True,'period':{'start':min(starts),'end':max(ends)},'chunkCount':len(chunks),'discoverySymbols':list(DISC),'holdoutSymbols':list(HOLD),'rawGate':{'meanGrossPctMin':1.5,'medianPositive':True,'hitRateMinExclusive':.55,'medianMFEtoMAEMin':2.0,'qualifyingSymbolsMin':2,'eventsPerSymbolMin':3,'discoveryPooledMin':20,'holdoutPooledMin':15},'results':results,'winners':winners,'next':'FREEZE_RESIDUAL_EVENT_AND_TIME_OOS' if winners else 'REJECT_GATE_E_V1'}
    p=pathlib.Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(out,indent=2));print(json.dumps({'period':out['period'],'winners':winners,'next':out['next']}))
if __name__=='__main__':main()
