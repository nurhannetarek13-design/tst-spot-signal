#!/usr/bin/env python3
from __future__ import annotations
import argparse,glob,json,pathlib
import numpy as np,pandas as pd
from research.gate_f_breadth_chunk_v1 import AUTH,HOURS,EVENTS,DISC,HOLD

def dedup(rows,h):
    rows=sorted(rows,key=lambda x:x['ts']);out=[];last=None
    for r in rows:
        ts=pd.Timestamp(r['ts'])
        if last is None or (ts-last).total_seconds()>=h*3600:out.append(r);last=ts
    return out

def stats(rows):
    if not rows:return {'n':0,'meanPct':None,'medianPct':None,'hitRate':None,'mfeMaeRatio':None}
    r=np.array([x['ret'] for x in rows]);mfe=np.array([x['mfe'] for x in rows]);mae=np.array([x['mae'] for x in rows]);mmfe=float(np.median(mfe));mmae=float(np.median(mae))
    return {'n':len(rows),'meanPct':float(r.mean()*100),'medianPct':float(np.median(r)*100),'hitRate':float((r>0).mean()),'mfeMaeRatio':float(mmfe/mmae) if mmae>0 else None}

def group(by,syms,ev,h):
    pooled=[];q=0
    for s in syms:
        rows=dedup(by.get(ev,{}).get(s,{}).get(str(h),[]),h)
        if len(rows)>=3:q+=1
        pooled+=rows
    st=stats(pooled);st['qualifyingSymbols']=q;return st

def passes(st,minn):
    return bool(st['n']>=minn and st['qualifyingSymbols']>=2 and st['meanPct']>=1.5 and st['medianPct']>0 and st['hitRate']>.55 and (st['mfeMaeRatio'] or 0)>=2)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--glob',default='gate-f-chunks/*.json');ap.add_argument('--out',default='validation/edges/gate-f-breadth-v1.json');a=ap.parse_args()
    files=glob.glob(a.glob);assert len(files)>=9,files;chunks=[json.load(open(f)) for f in files]
    by={}
    for c in chunks:
        assert c['authorization']==AUTH and c['liveTrading'] is False and c['thresholdsFrozen'] is True
        for ev in EVENTS:
            by.setdefault(ev,{})
            for s,hs in c['samples'][ev].items():
                by[ev].setdefault(s,{})
                for h,rows in hs.items():by[ev][s].setdefault(h,[]);by[ev][s][h]+=rows
    results={};w=[]
    for ev in EVENTS:
        results[ev]={}
        for h in HOURS:
            d=group(by,DISC,ev,h);o=group(by,HOLD,ev,h);ok=passes(d,18) and passes(o,15)
            results[ev][str(h)]={'discovery':d,'symbolHoldout':o,'rawGatePass':ok}
            if ok:w.append({'event':ev,'horizonHours':h})
    out={'engine':'GATE_F_BREADTH_DISCOVERY_V1','authorization':AUTH,'liveTrading':False,'usesML':False,'usesSizing':False,'thresholdsFrozen':True,'period':{'start':min(c['coreStart'] for c in chunks),'end':max(c['coreEnd'] for c in chunks)},'chunkCount':len(chunks),'results':results,'winners':w,'next':'FREEZE_BREADTH_EVENT_AND_TIME_OOS' if w else 'REJECT_GATE_F_V1'}
    p=pathlib.Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(out,indent=2));print(json.dumps({'winners':w,'next':out['next'],'period':out['period']}))
if __name__=='__main__':main()
