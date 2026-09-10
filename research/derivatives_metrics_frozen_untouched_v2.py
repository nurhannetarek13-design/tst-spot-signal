#!/usr/bin/env python3
"""Untouched validation for the frozen survivors from metrics-vs-Spot V2.

The candidate list is frozen from the 2025-01-01..2025-01-28 Discovery/OOS
scan. This script does not search thresholds or features. It evaluates only the
frozen candidates on a later untouched window and applies realistic round-trip
cost gates plus an overlap/de-clustering robustness check.
"""
from __future__ import annotations

import argparse, json, math, pathlib
from datetime import datetime

import numpy as np
import pandas as pd

from research.historical_derivatives_metrics_raw_edge_v1 import (
    Z_TAIL, load_metrics, load_spot_5m, prepare,
)

AUTHORIZATION='RESEARCH_ONLY'
NORMAL_COST=0.0028
STRESS_COST=0.0050
MIN_EVENTS=20
FROZEN={
 'BTCUSDT':[
  ('oi_value_chg_1h','LOW','15m'),
  ('oi_value_chg_1h','LOW','60m'),
 ],
 'ETHUSDT':[
  ('oi_contracts_chg_1h','LOW','15m'),
  ('oi_value_chg_1h','LOW','15m'),
  ('taker_long_short_log','LOW','15m'),
 ],
 'SOLUSDT':[
  ('oi_value_chg_1h','LOW','60m'),
  ('toptrader_accounts_log','LOW','240m'),
  ('global_long_short_log','LOW','240m'),
 ],
 'XRPUSDT':[
  ('toptrader_positions_log','HIGH','240m'),
 ],
}
HORIZON_MIN={'15m':15,'60m':60,'240m':240}


def stats(values,cost=0.0):
 v=pd.to_numeric(values,errors='coerce').dropna().astype(float)-cost
 if len(v)==0:return {'n':0,'mean':None,'median':None,'hitRate':None,'profitFactor':None,'pMeanLe0':None}
 pos=float(v[v>0].sum()); neg=float(-v[v<0].sum())
 mean=float(v.mean()); sd=float(v.std(ddof=1)) if len(v)>1 else 0.0
 if len(v)>1 and sd>0:
  z=mean/(sd/math.sqrt(len(v)))
  p=0.5*math.erfc(z/math.sqrt(2.0))
 else:
  p=0.0 if mean>0 else 1.0
 return {
  'n':int(len(v)),'mean':mean,'median':float(v.median()),
  'hitRate':float((v>0).mean()),
  'profitFactor':float(pos/neg if neg>0 else (999.0 if pos>0 else 0.0)),
  'pMeanLe0':float(p),
 }


def gross_gate(m):
 return bool(m['n']>=MIN_EVENTS and m['mean']>0 and m['median']>0 and m['hitRate']>0.55 and m['profitFactor']>=1.20)


def net_gate(m):
 return bool(m['n']>=MIN_EVENTS and m['mean']>0 and m['median']>0 and m['hitRate']>0.50 and m['profitFactor']>=1.10)


def decluster(idx:pd.DatetimeIndex,min_minutes:int):
 keep=[]; last=None
 for ts in idx:
  if last is None or (ts-last)>=pd.Timedelta(minutes=min_minutes):
   keep.append(ts); last=ts
 return pd.DatetimeIndex(keep)


def bh_qvalues(pvals):
 n=len(pvals); order=sorted(range(n),key=lambda i:pvals[i]); q=[1.0]*n; prev=1.0
 for rank_i in range(n-1,-1,-1):
  i=order[rank_i]; rank=rank_i+1; val=min(prev,pvals[i]*n/rank); q[i]=val; prev=val
 return q


def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--symbol',required=True); ap.add_argument('--warm-start',default='2025-01-15'); ap.add_argument('--eval-start',default='2025-01-29'); ap.add_argument('--end',default='2025-02-11'); ap.add_argument('--output-dir',default='artifacts/derivatives-metrics-frozen-v2'); a=ap.parse_args()
 s=a.symbol.upper()
 if s not in FROZEN: raise SystemExit(f'no frozen candidates for {s}')
 start=datetime.strptime(a.warm_start,'%Y-%m-%d').date(); end=datetime.strptime(a.end,'%Y-%m-%d').date(); eval_start=pd.Timestamp(a.eval_start,tz='UTC')
 md=load_metrics(s,start,end); px=load_spot_5m(s,start,end); x=prepare(md,px); e=x[x.ts>=eval_start].copy()
 results=[]
 for feature,side,horizon in FROZEN[s]:
  mask=e[feature+'__z']<=-Z_TAIL if side=='LOW' else e[feature+'__z']>=Z_TAIL
  ev=e.loc[mask,['ts','fwd_'+horizon]].dropna().copy().set_index('ts')
  g=stats(ev['fwd_'+horizon]); n=stats(ev['fwd_'+horizon],NORMAL_COST); st=stats(ev['fwd_'+horizon],STRESS_COST)
  di=decluster(ev.index,HORIZON_MIN[horizon]); dev=ev.loc[di]
  dg=stats(dev['fwd_'+horizon]); dn=stats(dev['fwd_'+horizon],NORMAL_COST); dst=stats(dev['fwd_'+horizon],STRESS_COST)
  results.append({
   'feature':feature,'side':side,'horizon':horizon,'threshold':f"z {'<=' if side=='LOW' else '>='} {-Z_TAIL if side=='LOW' else Z_TAIL}",
   'allEvents':{'gross':g,'normalNet':n,'stressNet':st},
   'declustered':{'minimumSpacingMinutes':HORIZON_MIN[horizon],'gross':dg,'normalNet':dn,'stressNet':dst},
   'grossPass':gross_gate(g),
   'normalNetPass':net_gate(n),
   'stressNetPass':net_gate(st),
   'declusteredNormalNetPass':net_gate(dn),
  })
 pvals=[r['allEvents']['normalNet']['pMeanLe0'] if r['allEvents']['normalNet']['pMeanLe0'] is not None else 1.0 for r in results]
 qvals=bh_qvalues(pvals)
 for r,q in zip(results,qvals):
  r['normalNetBHq']=q
  r['productionGatePass']=bool(r['grossPass'] and r['normalNetPass'] and r['stressNetPass'] and r['declusteredNormalNetPass'] and q<=0.05)
 report={
  'engine':'DERIVATIVES_METRICS_FROZEN_UNTOUCHED_V2','authorization':AUTHORIZATION,'liveTrading':False,'automaticPromotion':False,
  'selectionSourceWindow':'2025-01-01..2025-01-28','warmupWindow':a.warm_start+'..'+a.eval_start,'untouchedEvaluationWindow':a.eval_start+'..'+a.end,
  'symbol':s,'costs':{'normalRoundTrip':NORMAL_COST,'stressRoundTrip':STRESS_COST},'multipleTesting':'BH-FDR across frozen candidates within symbol',
  'frozenCandidates':[{'feature':f,'side':sd,'horizon':h} for f,sd,h in FROZEN[s]],'results':results,
  'productionPassCount':sum(r['productionGatePass'] for r in results),'productionCandidate':any(r['productionGatePass'] for r in results),
 }
 out=pathlib.Path(a.output_dir); out.mkdir(parents=True,exist_ok=True); p=out/f'{s}-frozen-untouched-v2.json'; p.write_text(json.dumps(report,indent=2,sort_keys=True)); print(json.dumps({'symbol':s,'productionPassCount':report['productionPassCount'],'results':[(r['feature'],r['side'],r['horizon'],r['grossPass'],r['normalNetPass'],r['stressNetPass'],r['declusteredNormalNetPass'],r['normalNetBHq']) for r in results]},separators=(',',':')))

if __name__=='__main__': main()
