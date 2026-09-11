#!/usr/bin/env python3
"""Gate H V1: persistent L2 pressure from Bybit ob500 archives.
Research-only. Frozen definitions. No ML, sizing, or live execution.
"""
from __future__ import annotations
import argparse,io,json,pathlib,urllib.request,zipfile
import numpy as np
BASE='https://quote-saver.bycsi.com/orderbook/linear'
H=(10,30,60)
PERSIST=5
OBI_MIN=0.45
MICRO_MIN_BPS=0.05
COOLDOWN=60

def topn(book:dict[float,float],reverse:bool,n=10):
    return sorted(((p,q) for p,q in book.items() if q>0),key=lambda x:x[0],reverse=reverse)[:n]

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--symbol',required=True);ap.add_argument('--date',required=True);ap.add_argument('--out',required=True);a=ap.parse_args()
    url=f'{BASE}/{a.symbol}/{a.date}_{a.symbol}_ob500.data.zip'
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req,timeout=240) as r:blob=r.read()
    z=zipfile.ZipFile(io.BytesIO(blob));name=z.namelist()[0]
    bids={};asks={};samples=[];next_sample=None
    with z.open(name) as f:
        for raw in f:
            try:o=json.loads(raw);ts=int(o.get('ts') or 0);d=o['data']
            except Exception:continue
            if o.get('type')=='snapshot':
                bids={float(p):float(q) for p,q in d.get('b',[])};asks={float(p):float(q) for p,q in d.get('a',[])}
            else:
                for p,q in d.get('b',[]):
                    p=float(p);q=float(q); bids.pop(p,None) if q==0 else bids.__setitem__(p,q)
                for p,q in d.get('a',[]):
                    p=float(p);q=float(q); asks.pop(p,None) if q==0 else asks.__setitem__(p,q)
            if not bids or not asks:continue
            if next_sample is None:next_sample=((ts//1000)+1)*1000
            if ts<next_sample:continue
            b=topn(bids,True,10);aa=topn(asks,False,10)
            if not b or not aa:continue
            bp,bq=b[0];apx,aq=aa[0];mid=(bp+apx)/2
            bsum=sum(q for _,q in b);asum=sum(q for _,q in aa);den=bsum+asum
            obi=(bsum-asum)/den if den else 0.0
            micro=(apx*bq+bp*aq)/(bq+aq) if (bq+aq)>0 else mid
            micro_bps=(micro-mid)/mid*1e4
            samples.append((ts,mid,obi,micro_bps))
            next_sample=((ts//1000)+1)*1000
    arr=np.array(samples,float)
    outcomes={}
    for side in ('long','short'):
        qualifies=np.zeros(len(arr),dtype=bool)
        for i in range(PERSIST-1,len(arr)):
            w=arr[i-PERSIST+1:i+1]
            if side=='long':
                qualifies[i]=bool(np.all(w[:,2]>=OBI_MIN) and np.mean(w[:,3])>=MICRO_MIN_BPS)
            else:
                qualifies[i]=bool(np.all(w[:,2]<=-OBI_MIN) and np.mean(w[:,3])<=-MICRO_MIN_BPS)
        idx=np.flatnonzero(qualifies);keep=[];last=-10**9
        for i in idx:
            if i-last>=COOLDOWN:keep.append(i);last=i
        outcomes[side]={}
        for h in H:
            vals=[]
            for i in keep:
                if i+h>=len(arr):continue
                r=(arr[i+h,1]/arr[i,1]-1)*1e4
                vals.append(r if side=='long' else -r)
            x=np.array(vals,float)
            outcomes[side][str(h)]={'n':int(len(x)),'meanDirectionalBps':float(x.mean()) if len(x) else None,'medianDirectionalBps':float(np.median(x)) if len(x) else None,'hitRate':float((x>0).mean()) if len(x) else None}
    result={'engine':'GATE_H_L2_PERSISTENT_PRESSURE_V1','authorization':'RESEARCH_ONLY','liveTrading':False,'usesML':False,'usesSizing':False,'thresholdsFrozen':True,'symbol':a.symbol,'date':a.date,'archiveBytes':len(blob),'sampleCount':len(samples),'eventDefinition':{'persistenceSeconds':PERSIST,'obi10AbsMin':OBI_MIN,'meanMicropriceDirectionalBpsMin':MICRO_MIN_BPS,'sampleSeconds':1,'eventCooldownSeconds':COOLDOWN},'outcomes':outcomes}
    p=pathlib.Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(result,indent=2));print(json.dumps({'symbol':a.symbol,'date':a.date,'samples':len(samples),'outcomes':outcomes}))
if __name__=='__main__':main()
