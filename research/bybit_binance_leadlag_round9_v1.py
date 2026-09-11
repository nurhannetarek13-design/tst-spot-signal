#!/usr/bin/env python3
"""Round 9 frozen cross-exchange discovery: Bybit -> Binance Spot lead/lag.

Reuses the already-tested archive parsers from bybit_binance_leadlag_v1.py.
Research only. Definitions below are frozen before results.
"""
from __future__ import annotations
import importlib.util, json, math, os, pathlib, time
from datetime import date, timedelta
import numpy as np
import pandas as pd

HERE=pathlib.Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('plumbing', HERE/'bybit_binance_leadlag_v1.py')
p=importlib.util.module_from_spec(spec); assert spec.loader is not None; spec.loader.exec_module(p)

SYMBOLS=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT']
START='2025-01-15'; DAYS=3
HORIZONS={'4h':1440,'12h':4320,'24h':8640}  # 10-second bars
MIN_EVENTS=40; MIN_MEAN=.015; MIN_HIT=.55; MIN_MFE_MAE=2.0; FDR_Q=.05
MIN_POSITIVE_SYMBOLS=4; MAX_EVENT_SHARE=.40; LOO_MIN_MEAN=0.; LOO_MIN_HIT=.50
DECLUSTER_SEC=3600
# Frozen event definitions.
IMPULSE_BARS=6                 # 60 seconds
IMPULSE_ABS=.0015              # 15 bps Bybit impulse
UNDERREACTION_RATIO=.40        # Binance moved <=40% of Bybit impulse
IMBALANCE_ABS=.25
RESID_BARS=30                  # 5 minutes
RESID_Z_LOOKBACK=2160          # 6 hours on 10s grid
RESID_Z_MIN=3.0
MAX_SPREAD_BPS=8.0
MIN_GRID_COVERAGE=.95
OUT=pathlib.Path('validation/edges/bybit-binance-leadlag-round9-v1-latest.json')

def zscore(s,w,minp):
    m=s.rolling(w,min_periods=minp).mean(); sd=s.rolling(w,min_periods=minp).std(ddof=0).replace(0,np.nan)
    return (s-m)/sd

def decluster_idx(mask,ts):
    out=[]; last=None
    for i in np.flatnonzero(mask.fillna(False).to_numpy()):
        t=int(ts.iloc[i])
        if last is None or t-last>=DECLUSTER_SEC*1000: out.append(i); last=t
    return out

def one_sided_p(a):
    if len(a)<2:return 1.0
    sd=float(np.std(a,ddof=1))
    if not np.isfinite(sd) or sd<=0:return 0.0 if float(np.mean(a))>0 else 1.0
    z=float(np.mean(a))/(sd/math.sqrt(len(a))); return 0.5*math.erfc(z/math.sqrt(2))

def calc(records,h,drop=None):
    vals=[];mfe=[];mae=[];syms=[]
    for sym,x,i,sign in records:
        if sym==drop or i+h>=len(x):continue
        e=float(x.spot.iloc[i]); f=float(x.spot.iloc[i+h])
        if not np.isfinite(e) or not np.isfinite(f) or e<=0:continue
        future=x.iloc[i+1:i+h+1]
        if len(future)<h or future.spot.isna().any():continue
        r=(f/e-1)*sign; vals.append(r); syms.append(sym)
        hi=float(future.spot.max()/e-1); lo=float(1-future.spot.min()/e)
        if sign>0:mfe.append(max(0.,hi));mae.append(max(0.,lo))
        else:mfe.append(max(0.,lo));mae.append(max(0.,hi))
    if not vals:return None
    a=np.asarray(vals,float); mf=np.asarray(mfe,float); ma=np.asarray(mae,float); med_mae=float(np.median(ma)); ratio=float(np.median(mf)/med_mae) if med_mae>0 else 999.
    per={}
    for s in sorted(set(syms)):
        q=np.asarray([v for v,ss in zip(vals,syms) if ss==s],float); per[s]={'n':int(len(q)),'mean':float(q.mean()),'hitRate':float((q>0).mean())}
    return {'n':int(len(a)),'mean':float(a.mean()),'median':float(np.median(a)),'hitRate':float((a>0).mean()),'medianMFE':float(np.median(mf)),'medianMAE':med_mae,'mfeMaeRatio':ratio,'pMeanPositive':one_sided_p(a),'perSymbol':per}

def robust(st,records,h):
    elig=[s for s,v in st['perSymbol'].items() if v['n']>=5]
    pos=sum(st['perSymbol'][s]['mean']>0 for s in elig)
    share=max((v['n']/st['n'] for v in st['perSymbol'].values()),default=1.)
    loo={}; ok=True
    for s in st['perSymbol']:
        q=calc(records,h,drop=s)
        if q:
            loo[s]={'n':q['n'],'mean':q['mean'],'hitRate':q['hitRate']}
            if not(q['mean']>LOO_MIN_MEAN and q['hitRate']>LOO_MIN_HIT):ok=False
    passed=pos>=MIN_POSITIVE_SYMBOLS and share<=MAX_EVENT_SHARE and ok
    return {'eligibleSymbols':elig,'positiveSymbols':pos,'maxEventShare':share,'leaveOneOut':loo,'robustPass':passed}

def bh(rows):
    if not rows:return
    order=sorted(range(len(rows)),key=lambda i:rows[i]['pMeanPositive']); m=len(rows); adj=[1.]*m; run=1.
    for rank in range(m,0,-1):
        i=order[rank-1]; run=min(run,rows[i]['pMeanPositive']*m/rank); adj[i]=min(1.,run)
    for i,r in enumerate(rows):r['qValue']=adj[i];r['pass']=bool(r['rawPass'] and r['robustness']['robustPass'] and adj[i]<=FDR_Q)

def load_symbol(sym):
    root=pathlib.Path('/tmp/round9-cross-exchange')/sym; frames=[]; quality=[]; d=date.fromisoformat(START)
    for j in range(DAYS):
        ds=(d+timedelta(days=j)).isoformat()
        bp=root/f'{ds}-bybit.zip'; sp=root/f'{ds}-spot.zip'
        p.download(f'{p.BYBIT}/{sym}/{ds}_{sym}_ob500.data.zip',bp); b,q=p.bybit_day(bp); os.remove(bp)
        p.download(f'{p.VISION}/{sym}/{sym}-aggTrades-{ds}.zip',sp); s=p.binance_spot_day(sp); os.remove(sp)
        x=pd.merge(b,s,on='ts_ms',how='inner').sort_values('ts_ms'); frames.append(x)
        expected=24*60*6; quality.append({'date':ds,**q,'mergedRows':int(len(x)),'mergedCoverage':float(len(x)/expected)})
    x=pd.concat(frames,ignore_index=True).sort_values('ts_ms').drop_duplicates('ts_ms').reset_index(drop=True)
    expected=DAYS*24*60*6; cov=float(len(x)/expected)
    return x,quality,cov

def main():
    t0=time.time(); frames={}; quality={}; excluded={}
    for sym in SYMBOLS:
        print(f'LOAD {sym}',flush=True)
        try:x,q,cov=load_symbol(sym)
        except Exception as e:excluded[sym]=f'LOAD_FAIL:{e}';print(f'{sym} FAIL {e}',flush=True);continue
        quality[sym]={'coverage':cov,'days':q}
        if cov<MIN_GRID_COVERAGE:excluded[sym]=f'GRID_COVERAGE_{cov:.4f}';continue
        x['bybit_imp']=x.mid/x.mid.shift(IMPULSE_BARS)-1; x['binance_imp']=x.spot/x.spot.shift(IMPULSE_BARS)-1
        x['bybit_r5m']=x.mid/x.mid.shift(RESID_BARS)-1; x['binance_r5m']=x.spot/x.spot.shift(RESID_BARS)-1
        x['resid']=x.bybit_r5m-x.binance_r5m; x['resid_z']=zscore(x.resid,RESID_Z_LOOKBACK,540)
        frames[sym]=x; print(f'{sym} OK coverage={cov:.4f}',flush=True)
    pools={k:[] for k in ['BYBIT_IMPULSE_BINANCE_UNDERREACTION_V1','CROSS_EXCHANGE_RESIDUAL_DIVERGENCE_CONT_V1','CROSS_EXCHANGE_RESIDUAL_DIVERGENCE_REV_V1']}
    event_counts={}
    for sym,x in frames.items():
        spread=x.spread_bps<=MAX_SPREAD_BPS
        up=(x.bybit_imp>=IMPULSE_ABS)&(x.binance_imp>=0)&(x.binance_imp<=x.bybit_imp*UNDERREACTION_RATIO)&(x.imb10>=IMBALANCE_ABS)&spread
        dn=(x.bybit_imp<=-IMPULSE_ABS)&(x.binance_imp<=0)&(x.binance_imp>=x.bybit_imp*UNDERREACTION_RATIO)&(x.imb10<=-IMBALANCE_ABS)&spread
        for i in decluster_idx(up,x.ts_ms):pools['BYBIT_IMPULSE_BINANCE_UNDERREACTION_V1'].append((sym,x,i,1))
        for i in decluster_idx(dn,x.ts_ms):pools['BYBIT_IMPULSE_BINANCE_UNDERREACTION_V1'].append((sym,x,i,-1))
        pos=(x.resid_z>=RESID_Z_MIN)&(x.imb10>=IMBALANCE_ABS)&spread
        neg=(x.resid_z<=-RESID_Z_MIN)&(x.imb10<=-IMBALANCE_ABS)&spread
        for i in decluster_idx(pos,x.ts_ms):
            pools['CROSS_EXCHANGE_RESIDUAL_DIVERGENCE_CONT_V1'].append((sym,x,i,1));pools['CROSS_EXCHANGE_RESIDUAL_DIVERGENCE_REV_V1'].append((sym,x,i,-1))
        for i in decluster_idx(neg,x.ts_ms):
            pools['CROSS_EXCHANGE_RESIDUAL_DIVERGENCE_CONT_V1'].append((sym,x,i,-1));pools['CROSS_EXCHANGE_RESIDUAL_DIVERGENCE_REV_V1'].append((sym,x,i,1))
    tests=[]
    for fam,recs in pools.items():
        event_counts[fam]=len(recs); print(f'{fam} events={len(recs)}',flush=True)
        for label,h in HORIZONS.items():
            st=calc(recs,h)
            if not st:continue
            rb=robust(st,recs,h)
            raw=bool(st['n']>=MIN_EVENTS and st['mean']>=MIN_MEAN and st['median']>0 and st['hitRate']>MIN_HIT and st['mfeMaeRatio']>=MIN_MFE_MAE)
            tests.append({'family':fam,'horizon':label,'bars10s':h,**st,'rawPass':raw,'robustness':rb})
    bh(tests);surv=[r for r in tests if r['pass']]
    enough_symbols=len(frames)>=4
    decision='INSUFFICIENT_DATA' if not enough_symbols else ('SURVIVOR_NEEDS_FROZEN_OOS' if surv else 'NO_EDGE_FOUND')
    out={'engine':'BYBIT_BINANCE_LEADLAG_ROUND9_V1','round':9,'authorization':'RESEARCH_ONLY','liveTrading':False,'liveReady':False,
      'period':{'start':START,'days':DAYS},'symbolsRequested':SYMBOLS,'symbolsLoaded':sorted(frames),'symbolsExcluded':excluded,'quality':quality,
      'families':list(pools),'frozenDefinitions':{'impulseBars10s':IMPULSE_BARS,'impulseAbs':IMPULSE_ABS,'underreactionRatio':UNDERREACTION_RATIO,'imbalanceAbs':IMBALANCE_ABS,'residualBars10s':RESID_BARS,'residualZLookbackBars10s':RESID_Z_LOOKBACK,'residualZMin':RESID_Z_MIN,'maxSpreadBps':MAX_SPREAD_BPS,'declusterSec':DECLUSTER_SEC},
      'rawGate':{'minimumEvents':MIN_EVENTS,'meanMin':MIN_MEAN,'medianPositive':True,'hitRateMinExclusive':MIN_HIT,'mfeMaeMin':MIN_MFE_MAE},
      'robustnessGate':{'positiveSymbolsMin':MIN_POSITIVE_SYMBOLS,'maxEventShare':MAX_EVENT_SHARE,'leaveOneOutMeanMinExclusive':LOO_MIN_MEAN,'leaveOneOutHitRateMinExclusive':LOO_MIN_HIT},
      'multipleTesting':{'method':'Benjamini-Hochberg','q':FDR_Q},'eventCounts':event_counts,'tests':tests,'survivors':surv,'survivorCount':len(surv),'decision':decision,'elapsedSec':time.time()-t0}
    OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(out,indent=2,sort_keys=True),encoding='utf-8');print(json.dumps(out,indent=2,sort_keys=True))
if __name__=='__main__':main()
