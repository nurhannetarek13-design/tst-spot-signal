#!/usr/bin/env python3
"""Research-only multi-factor derivatives candidate gate.

Fixed, predeclared candidate families using Binance Vision historical futures data:
1) PRESSURE_CONTINUATION: positive taker flow + rising OI + non-overheated basis + BTC up regime.
2) DELEVERAGING_REVERSAL: sharp selloff + negative flow + falling OI + discounted basis + BTC not crashing.
3) FLOW_OI_DIVERGENCE: positive flow while OI contracts after a mild pullback + BTC up regime.

Chronological split is 50% discovery / 25% validation / 25% OOS. All reported
returns charge round-trip costs. No optimization, account access, orders, or live authorization.
"""
from __future__ import annotations
import json, pathlib
from datetime import datetime
import numpy as np
import pandas as pd
from research.binance_vision_historical_feature_layer import build

AUTH='RESEARCH_ONLY'
START=datetime(2026,8,12).date(); END=datetime(2026,9,8).date(); FREQ='15min'
TRADE_SYMBOLS=('ETHUSDT','SOLUSDT'); BTC='BTCUSDT'
HORIZONS={'1h':4,'4h':16,'12h':48}
NORMAL_COST=.0028; STRESS_COST=.0050; MIN_N=12
OUT=pathlib.Path('validation/edges/multifactor-derivatives-candidate-gate-v1.json')

def z(s,n=96):
    m=s.rolling(n,min_periods=n//2).mean(); sd=s.rolling(n,min_periods=n//2).std(ddof=0).replace(0,np.nan)
    return (s-m)/sd

def prep(df):
    x=df.copy(); x['ts']=pd.to_datetime(x.ts,utc=True); x=x.set_index('ts').sort_index()
    x['ret_1h']=x.trade_close.pct_change(4)
    x['flow_z']=z(x.flow_imbalance_quote)
    x['oi_z']=z(x.fut_oi_change_1bar)
    x['basis_z']=z(x.mark_index_basis_bps)
    x['taker_z']=z(np.log(x.fut_sum_taker_long_short_vol_ratio.replace(0,np.nan)))
    return x

def btc_regime(b):
    r=b.trade_close.pct_change(16); ma=b.trade_close.rolling(96).mean()
    return pd.DataFrame({'btc_up':(r>0)&(b.trade_close>ma),'btc_not_crash':r>-0.03},index=b.index)

def masks(x,r):
    return {
      'PRESSURE_CONTINUATION': (x.flow_z>=1.0)&(x.oi_z>=0.5)&(x.taker_z>=0.5)&(x.basis_z<1.5)&r.btc_up,
      'DELEVERAGING_REVERSAL': (x.ret_1h<=-0.02)&(x.flow_z<=-1.0)&(x.oi_z<=-0.5)&(x.basis_z<=-0.5)&r.btc_not_crash,
      'FLOW_OI_DIVERGENCE': (x.ret_1h.between(-0.02,0))&(x.flow_z>=1.0)&(x.oi_z<=-0.5)&(x.taker_z>=0)&r.btc_up,
    }

def decluster(ix,gap):
    out=[]; last=-10**9
    for i in ix:
        if i-last>=gap: out.append(i); last=i
    return out

def metrics(vals):
    a=np.asarray(vals,float)
    if not len(a): return {'n':0}
    gp=float(a[a>0].sum()); gl=float(-a[a<0].sum()); pf=gp/gl if gl>0 else 99.0
    return {'n':int(len(a)),'mean':float(a.mean()),'median':float(np.median(a)),'hitRate':float((a>0).mean()),'profitFactor':float(pf)}

def segment_values(x,mask,a,b,h,cost):
    ii=np.flatnonzero(mask.to_numpy()[a:b])+a; ii=decluster(ii,max(1,h//2)); vals=[]
    for i in ii:
        if i+h>=len(x) or i+h>=b: continue
        vals.append(float(x.trade_close.iloc[i+h]/x.trade_close.iloc[i]-1-cost))
    return vals

def gate(m,stress=False):
    return bool(m.get('n',0)>=MIN_N and m.get('mean',-1)>0 and m.get('median',-1)>0 and m.get('hitRate',0)>=.55 and m.get('profitFactor',0)>=(1.15 if stress else 1.25))

def main():
    frames={}; metas={}
    for s in (BTC,)+TRADE_SYMBOLS:
        f,m=build(s,START,END,FREQ,True)
        if m['failures']: raise RuntimeError(f'{s} archive failures: {m["failures"][:3]}')
        frames[s]=prep(f); metas[s]=m
    common=frames[BTC].index
    for s in TRADE_SYMBOLS: common=common.intersection(frames[s].index)
    btc=frames[BTC].reindex(common); reg=btc_regime(btc)
    rows=[]
    for s in TRADE_SYMBOLS:
        x=frames[s].reindex(common).dropna(subset=['trade_close']); rr=reg.reindex(x.index); n=len(x); c1=int(n*.5); c2=int(n*.75)
        for fam,mask in masks(x,rr).items():
            for hn,h in HORIZONS.items():
                d=metrics(segment_values(x,mask,0,c1,h,NORMAL_COST)); v=metrics(segment_values(x,mask,c1,c2,h,NORMAL_COST)); o=metrics(segment_values(x,mask,c2,n,h,NORMAL_COST)); st=metrics(segment_values(x,mask,c2,n,h,STRESS_COST))
                passed=gate(d) and gate(v) and gate(o) and gate(st,True)
                rows.append({'symbol':s,'family':fam,'horizon':hn,'discovery':d,'validation':v,'oos':o,'stressOos':st,'pass':passed})
    survivors=sorted([r for r in rows if r['pass']],key=lambda r:(-r['stressOos']['profitFactor'],-r['oos']['mean']))
    out={'engine':'MULTIFACTOR_DERIVATIVES_CANDIDATE_GATE_V1','authorization':AUTH,'liveTrading':False,'automaticPromotion':False,'window':{'start':str(START),'end':str(END),'frequency':FREQ,'split':'50/25/25 chronological'},'symbols':list(TRADE_SYMBOLS),'btcRegime':BTC,'costs':{'normalRoundTrip':NORMAL_COST,'stressRoundTrip':STRESS_COST},'families':['PRESSURE_CONTINUATION','DELEVERAGING_REVERSAL','FLOW_OI_DIVERGENCE'],'tests':len(rows),'results':rows,'survivorCount':len(survivors),'survivors':survivors,'productionCandidate':False,'dataRows':{s:metas[s]['rows'] for s in metas},'nextGate':'A survivor still requires a longer non-overlapping archive OOS and forward shadow. Zero survivors closes these frozen definitions.'}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2,sort_keys=True)); print(json.dumps({'engine':out['engine'],'tests':len(rows),'survivors':len(survivors),'dataRows':out['dataRows']}))
if __name__=='__main__': main()
