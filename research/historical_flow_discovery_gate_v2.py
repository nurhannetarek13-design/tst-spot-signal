#!/usr/bin/env python3
"""De-clustered historical flow discovery gate v2.

This replaces v1's overlapping-forward-window discovery. Thresholds are still
calibrated on the first 60% of each symbol and evaluated on the final 40%, but
events are now separated by at least the full forward horizon before statistics
are computed. Uses exact sign tests and Benjamini-Hochberg FDR. Research-only.
"""
from __future__ import annotations

import argparse, glob, json, math, pathlib
import numpy as np
import pandas as pd

AUTHORIZATION="RESEARCH_ONLY"
TRAIN_FRAC=0.60
LOW_Q=0.10
HIGH_Q=0.90
FDR_ALPHA=0.05
MIN_EVENTS=12
HORIZONS={"15m":3,"1h":12,"4h":48}
FEATURES=["flow_imbalance_quote","buy_share_quote","taker_buy_sell_ratio_quote","mark_index_basis_bps","premium_close","agg_trade_count"]


def exact_binom_greater(wins:int,n:int)->float:
    if n<=0:return 1.0
    return min(1.0,sum(math.comb(n,k) for k in range(wins,n+1))/(2**n))


def bh(pvals):
    n=len(pvals)
    if not n:return []
    order=np.argsort(np.asarray(pvals,float)); out=np.ones(n); prev=1.0
    for r0 in range(n-1,-1,-1):
        idx=int(order[r0]); rank=r0+1; q=min(prev,float(pvals[idx])*n/rank,1.0); out[idx]=q; prev=q
    return out.tolist()


def metrics(vals):
    a=np.asarray(vals,float); a=a[np.isfinite(a)]
    if len(a)==0:return {"n":0,"mean":None,"median":None,"hitRate":None,"signP":1.0}
    wins=int((a>0).sum()); nz=int((a!=0).sum())
    return {"n":int(len(a)),"mean":float(a.mean()),"median":float(np.median(a)),"hitRate":float((a>0).mean()),"signP":exact_binom_greater(wins,nz)}


def decluster(mask:np.ndarray,bars:int)->list[int]:
    idx=[]; i=0; n=len(mask)-bars
    while i<n:
        if bool(mask[i]): idx.append(i); i+=bars
        else:i+=1
    return idx


def process(d):
    d=d.sort_values('ts').reset_index(drop=True).copy(); symbol=str(d['symbol'].iloc[0]); n=len(d); cut=max(1,int(n*TRAIN_FRAC))
    train=d.iloc[:cut].copy(); test=d.iloc[cut:].copy().reset_index(drop=True)
    close=pd.to_numeric(test['trade_close'],errors='coerce').to_numpy(float)
    thresholds={}; rows=[]
    for feature in FEATURES:
        if feature not in d.columns:continue
        tr=pd.to_numeric(train[feature],errors='coerce').replace([np.inf,-np.inf],np.nan).dropna()
        if len(tr)<50:continue
        lo=float(tr.quantile(LOW_Q)); hi=float(tr.quantile(HIGH_Q)); thresholds[feature]={"q10":lo,"q90":hi}
        tv=pd.to_numeric(test[feature],errors='coerce').to_numpy(float)
        for tail,thr,cmp in (("LOW",lo,"le"),("HIGH",hi,"ge")):
            mask=np.isfinite(tv)&((tv<=thr) if cmp=='le' else (tv>=thr))
            for hname,bars in HORIZONS.items():
                ids=decluster(mask,bars)
                raw=[]
                for i in ids:
                    j=i+bars
                    if j<len(close) and np.isfinite(close[i]) and close[i]>0 and np.isfinite(close[j]) and close[j]>0:
                        raw.append(close[j]/close[i]-1.0)
                raw=np.asarray(raw,float)
                for mode,direction in (("CONTINUATION",1 if tail=="HIGH" else -1),("REVERSAL",-1 if tail=="HIGH" else 1)):
                    m=metrics(raw*direction)
                    rows.append({"symbol":symbol,"feature":feature,"tail":tail,"mode":mode,"direction":"LONG" if direction==1 else "SHORT","horizon":hname,"declusterBars":bars,"threshold":thr,**m})
    meta={"symbol":symbol,"rows":n,"trainRows":len(train),"testRows":len(test),"thresholds":thresholds}
    return rows,meta


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input',default='artifacts/historical-flow-input/**/*.parquet'); ap.add_argument('--output',default='validation/edges/historical-flow-discovery-gate-v2.json'); a=ap.parse_args()
    files=sorted(glob.glob(a.input,recursive=True));
    if not files:raise SystemExit('no parquet inputs')
    tests=[]; symbols=[]
    for p in files:
        d=pd.read_parquet(p); d['ts']=pd.to_datetime(d['ts'],utc=True); r,m=process(d); tests+=r; symbols.append(m)
    qs=bh([r['signP'] for r in tests])
    for r,q in zip(tests,qs):
        r['qValueBH']=float(q); r['statisticalPass']=bool(r['n']>=MIN_EVENTS and r['mean'] is not None and r['mean']>0 and r['median'] is not None and r['median']>0 and r['hitRate'] is not None and r['hitRate']>0.60 and q<=FDR_ALPHA)
    survivors=[r for r in tests if r['statisticalPass']]
    survivors.sort(key=lambda r:(r['qValueBH'],-(r['mean'] or 0)))
    family_support={}
    for r in survivors:
        key='|'.join([r['feature'],r['tail'],r['mode'],r['direction'],r['horizon']]); family_support.setdefault(key,[]).append(r['symbol'])
    recurring={k:sorted(set(v)) for k,v in family_support.items() if len(set(v))>=3}
    out={"engine":"HISTORICAL_FLOW_DISCOVERY_GATE_V2","authorization":AUTHORIZATION,"liveTrading":False,"automaticPromotion":False,"researchStage":"DECLUSTERED_DISCOVERY_ONLY","supersedes":"HISTORICAL_FLOW_DISCOVERY_GATE_V1 due overlapping forward windows","dataSplit":{"trainFraction":TRAIN_FRAC,"testFraction":1-TRAIN_FRAC},"frozenQuantiles":{"low":LOW_Q,"high":HIGH_Q},"horizons":HORIZONS,"features":FEATURES,"multipleTesting":{"method":"Benjamini-Hochberg over exact sign-test p-values","alpha":FDR_ALPHA},"gate":{"minEvents":MIN_EVENTS,"median":">0","hitRate":">0.60","bhFdr":"<=0.05","eventSpacing":"at least full horizon"},"symbols":symbols,"testCount":len(tests),"survivorCount":len(survivors),"recurringFamilyCount":len(recurring),"recurringFamilies":recurring,"survivors":survivors,"tests":tests,"nextStep":"Only recurring families may advance to frozen validation on a disjoint date range and unseen symbols. No live promotion."}
    p=pathlib.Path(a.output); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(out,indent=2,sort_keys=True,allow_nan=False))
    print(json.dumps({"engine":out['engine'],"tests":len(tests),"survivors":len(survivors),"recurringFamilies":recurring,"top":survivors[:10]},separators=(',',':'),allow_nan=False))
if __name__=='__main__':main()
