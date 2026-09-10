#!/usr/bin/env python3
from __future__ import annotations
import argparse,glob,json,pathlib
import numpy as np,pandas as pd
from research.gate_d_event_discovery_v1 import HOURS,AUTH
from research.gate_d_event_chunk_v2 import EVENTS
SYMS=['BTCUSDT','ETHUSDT','SOLUSDT']
def dedup(rows,h):
    rows=sorted(rows,key=lambda x:x['ts']);out=[];last=None
    for r in rows:
        ts=pd.Timestamp(r['ts'])
        if last is None or (ts-last).total_seconds()>=h*3600:out.append(r);last=ts
    return out
def stats(rows):
    if not rows:return {'n':0,'meanPct':None,'medianPct':None,'hitRate':None,'medianMFEpct':None,'medianMAEpct':None,'mfeMaeRatio':None}
    r=np.array([x['ret'] for x in rows],float);mfe=np.array([x['mfe'] for x in rows],float);mae=np.array([x['mae'] for x in rows],float);mmfe=float(np.median(mfe));mmae=float(np.median(mae))
    return {'n':len(rows),'meanPct':float(r.mean()*100),'medianPct':float(np.median(r)*100),'hitRate':float((r>0).mean()),'medianMFEpct':mmfe*100,'medianMAEpct':mmae*100,'mfeMaeRatio':float(mmfe/mmae) if mmae>0 else None}
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--glob',default='chunks-v3/*.json');ap.add_argument('--out',default='validation/edges/gate-d-event-discovery-v3.json');a=ap.parse_args();files=sorted(glob.glob(a.glob));assert len(files)>=9,files
    chunks=[json.load(open(f)) for f in files]
    for c in chunks:assert c['authorization']==AUTH and c['liveTrading'] is False and c['thresholdsFrozen'] is True
    results={}
    for ev in EVENTS:
        per={}
        for s in SYMS:
            per[s]={}
            sc=[c for c in chunks if c['symbol']==s]
            assert len(sc)>=3,(s,len(sc))
            for h in HOURS:
                rows=[]
                for c in sc:rows+=c['samples'][ev][str(h)]
                per[s][str(h)]=stats(dedup(rows,h))
        passes=[]
        for h in HOURS:
            eligible=[per[s][str(h)] for s in SYMS if per[s][str(h)]['n']>=3]
            if len(eligible)>=2 and all(x['meanPct']>=1.5 and x['medianPct']>0 and x['hitRate']>.55 and (x['mfeMaeRatio'] or 0)>=2 for x in eligible):passes.append(h)
        results[ev]={'symbols':per,'rawGatePass':bool(passes),'passingHorizons':passes}
    winners=[e for e,v in results.items() if v['rawGatePass']];starts=[c['coreStart'] for c in chunks];ends=[c['coreEnd'] for c in chunks]
    out={'engine':'GATE_D_EVENT_DISCOVERY_V3_SYMBOL_CHUNKED','authorization':AUTH,'liveTrading':False,'usesML':False,'usesSizing':False,'thresholdsFrozen':True,'period':{'start':min(starts),'end':max(ends)},'chunkCount':len(chunks),'events':results,'winners':winners,'next':'FREEZE_EVENT_AND_OOS' if winners else 'REJECT_GATE_D_V3'}
    p=pathlib.Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(out,indent=2));print(json.dumps({'period':out['period'],'winners':winners,'next':out['next']}))
if __name__=='__main__':main()
