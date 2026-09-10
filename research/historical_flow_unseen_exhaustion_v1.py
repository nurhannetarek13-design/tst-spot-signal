#!/usr/bin/env python3
"""Unseen-symbol validation of the frozen trade-count exhaustion family.

Family definition was chosen before these symbols are evaluated:
  high 5m agg_trade_count = above symbol q90 from a disjoint calibration week
  expected next-4h direction = DOWN
  events de-clustered by 4h.

Research-only. If validated, production use is Spot risk-off/avoid-buy only.
"""
from __future__ import annotations

import argparse, glob, json, pathlib
import numpy as np
import pandas as pd
from scipy import stats

AUTHORIZATION="RESEARCH_ONLY"
H=48
Q=0.90


def metric(vals):
    a=np.asarray(vals,dtype=float); a=a[np.isfinite(a)]
    if len(a)==0: return {"n":0,"mean":None,"median":None,"hitRate":None,"signP":1.0}
    wins=int((a>0).sum()); nz=int((a!=0).sum())
    p=float(stats.binomtest(wins,nz,0.5,alternative='greater').pvalue) if nz else 1.0
    return {"n":int(len(a)),"mean":float(a.mean()),"median":float(np.median(a)),"hitRate":float((a>0).mean()),"signP":p}


def events(oos,thr):
    d=oos.sort_values('ts').reset_index(drop=True)
    c=pd.to_numeric(d['agg_trade_count'],errors='coerce').to_numpy(float)
    px=pd.to_numeric(d['trade_close'],errors='coerce').to_numpy(float)
    ts=pd.to_datetime(d['ts'],utc=True)
    out=[]; i=0
    while i < len(d)-H:
        if np.isfinite(c[i]) and c[i]>=thr and np.isfinite(px[i]) and px[i]>0 and np.isfinite(px[i+H]) and px[i+H]>0:
            lr=px[i+H]/px[i]-1
            out.append({"ts":ts.iloc[i].isoformat(),"tradeCount":float(c[i]),"threshold":float(thr),"longReturn4h":float(lr),"shortDirectionReturn4h":float(-lr)})
            i += H
        else: i += 1
    return out


def read_one(pattern):
    f=glob.glob(pattern,recursive=True)
    if len(f)!=1: raise RuntimeError(f'expected one parquet for {pattern}, got {f}')
    return pd.read_parquet(f[0])


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--root',required=True); ap.add_argument('--output',required=True); a=ap.parse_args()
    root=pathlib.Path(a.root); symbols=sorted(p.name for p in root.iterdir() if p.is_dir())
    rows=[]; pooled=[]; evmap={}
    for s in symbols:
        cal=read_one(str(root/s/'calibration'/'*.parquet')); oos=read_one(str(root/s/'oos'/'*.parquet'))
        x=pd.to_numeric(cal['agg_trade_count'],errors='coerce').replace([np.inf,-np.inf],np.nan).dropna()
        if len(x)<100: raise RuntimeError(f'{s}: insufficient calibration rows')
        thr=float(x.quantile(Q)); ev=events(oos,thr); vals=[z['shortDirectionReturn4h'] for z in ev]; m=metric(vals)
        rows.append({"symbol":s,"calibrationQ90":thr,**m}); pooled.extend(vals); evmap[s]=ev
    pm=metric(pooled); supporting=sum(1 for r in rows if r['n']>=2 and r['median'] is not None and r['median']>0 and r['hitRate']>0.5)
    passed=bool(pm['n']>=15 and pm['mean'] is not None and pm['mean']>0 and pm['median'] is not None and pm['median']>0 and pm['hitRate']>0.60 and pm['signP']<0.05 and supporting>=3)
    out={"engine":"HISTORICAL_FLOW_UNSEEN_EXHAUSTION_V1","authorization":AUTHORIZATION,"liveTrading":False,"futuresTrading":False,"automaticPromotion":False,"productionUseIfValidated":"SPOT_RISK_OFF_ONLY","family":{"feature":"agg_trade_count","thresholdMethod":"q90 on disjoint calibration week","quantile":Q,"expectedDirection":"DOWN","horizon":"4h","declusterMinutes":240},"symbols":rows,"pooled":pm,"supportingSymbols":supporting,"gate":{"minEvents":15,"hitRate":">0.60","mean":">0","median":">0","signP":"<0.05","minSupportingSymbols":3,"pass":passed},"events":evmap,"decision":"UNSEEN_SYMBOL_PASS" if passed else "UNSEEN_SYMBOL_FAIL"}
    p=pathlib.Path(a.output); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(out,indent=2,sort_keys=True,allow_nan=False))
    print(json.dumps({"engine":out['engine'],"pooled":pm,"supportingSymbols":supporting,"pass":passed,"symbols":rows},separators=(',',':'),allow_nan=False))
if __name__=='__main__': main()
