#!/usr/bin/env python3
"""Aggregate Gate D V2 chunk samples into one 90-day frozen raw-event gate."""
from __future__ import annotations
import argparse,glob,json,pathlib
import numpy as np,pandas as pd
from research.gate_d_event_discovery_v1 import HOURS,BARS,AUTH
EVENTS=['liquidity_crash_exhaustion','oi_dislocation_reversal','basis_flow_dislocation','vol_expansion_exhaustion']
SYMS=['BTCUSDT','ETHUSDT','SOLUSDT']

def dedup(samples,h):
    rows=sorted(samples,key=lambda x:x['ts']);keep=[];last=None
    for r in rows:
        ts=pd.Timestamp(r['ts'])
        if last is None or (ts-last).total_seconds()>=h*3600:
            keep.append(r);last=ts
    return keep

def stats(rows):
    if not rows:return {'n':0,'meanPct':None,'medianPct':None,'hitRate':None,'medianMFEpct':None,'medianMAEpct':None,'mfeMaeRatio':None}
    r=np.array([x['ret'] for x in rows],float);mfe=np.array([x['mfe'] for x in rows],float);mae=np.array([x['mae'] for x in rows],float)
    mmfe=float(np.median(mfe));mmae=float(np.median(mae))
    return {'n':len(rows),'meanPct':float(r.mean()*100),'medianPct':float(np.median(r)*100),'hitRate':float((r>0).mean()),'medianMFEpct':mmfe*100,'medianMAEpct':mmae*100,'mfeMaeRatio':float(mmfe/mmae) if mmae>0 else None}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--glob',default='chunks/*.json');ap.add_argument('--out',default='validation/edges/gate-d-event-discovery-v2.json');a=ap.parse_args()
    files=sorted(glob.glob(a.glob));assert len(files)>=3,files
    chunks=[json.load(open(f)) for f in files]
    for c in chunks:
        assert c['authorization']==AUTH and c['liveTrading'] is False and c['thresholdsFrozen'] is True
    results={}
    for ev in EVENTS:
        per={};passes=[]
        for s in SYMS:
            per[s]={}
            for h in HOURS:
                allrows=[]
                for c in chunks:allrows+=c['samples'][s][ev][str(h)]
                st=stats(dedup(allrows,h));per[s][str(h)]=st
        for h in HOURS:
            eligible=[per[s][str(h)] for s in SYMS if per[s][str(h)]['n']>=3]
            ok=bool(len(eligible)>=2 and all(x['meanPct']>=1.5 and x['medianPct']>0 and x['hitRate']>.55 and (x['mfeMaeRatio'] or 0)>=2 for x in eligible))
            if ok:passes.append(h)
        results[ev]={'symbols':per,'rawGatePass':bool(passes),'passingHorizons':passes}
    winners=[e for e,v in results.items() if v['rawGatePass']]
    starts=[c['coreStart'] for c in chunks];ends=[c['coreEnd'] for c in chunks]
    out={'engine':'GATE_D_EVENT_DISCOVERY_V2_CHUNKED','authorization':AUTH,'liveTrading':False,'usesML':False,'usesSizing':False,'thresholdsFrozen':True,'period':{'start':min(starts),'end':max(ends)},'chunkCount':len(chunks),'rawGate':{'meanGrossPctMin':1.5,'medianPositive':True,'hitRateMinExclusive':.55,'medianMFEtoMAEMin':2.0,'crossSymbolEligibleMin':2,'eventsPerSymbolHorizonMin':3},'events':results,'winners':winners,'next':'FREEZE_EVENT_AND_OOS' if winners else 'REJECT_GATE_D_V2'}
    p=pathlib.Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(out,indent=2));print(json.dumps({'period':out['period'],'winners':winners,'next':out['next']}))
if __name__=='__main__':main()
