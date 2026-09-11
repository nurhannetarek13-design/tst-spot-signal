#!/usr/bin/env python3
"""Round 6 orthogonal raw-edge discovery.

Frozen families before results:
1) BTC_SHOCK_ALT_UNDERREACTION_V1
2) RESIDUAL_EXTREME_REVERSAL_V1
3) VOLATILITY_REGIME_EXPANSION_V1
4) VOLUME_SHOCK_CONTINUATION_REVERSAL_V1

Research only. No strategy rules, costs, sizing, optimization, or live authorization.
"""
from __future__ import annotations
import json, math, pathlib, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import pandas as pd

BASE='https://data-api.binance.vision'
INTERVAL='1h'; DAYS=365; MAX_SYMBOLS=60; MIN_QV=10_000_000
HORIZONS=(24,48,72); MIN_EVENTS=40; MIN_GAP=24; FDR_Q=0.05
WORKERS=8; RETRIES=4
OUT=pathlib.Path('validation/edges/event-driven-edge-scanner-v2-latest.json')
EXCLUDED={'USDC','FDUSD','TUSD','USDP','DAI','BUSD','EUR','AEUR','TRY','BRL','GBP','AUD','USD1','RLUSD','USDE','PAXG','XAUT','U'}

# Frozen Round 6 definitions.
BTC_SHOCK_H=6; BTC_SHOCK_ABS=0.03; UNDERREACTION_RATIO=0.50
RESID_H=24; BETA_LOOKBACK=168; RESID_Z_LOOKBACK=240; RESID_Z=3.0
VOL_SHORT=6; VOL_BASE=72; VOL_LOW_Q=0.20; VOL_EXPAND_MULT=2.0; VOL_DIR_RET=0.01
VOL_Q_LOOKBACK=240
QV_LOOKBACK=168; QV_MULT=4.0; SHOCK_RET_H=6; SHOCK_RET=0.02

def log(s): print(f"[{time.strftime('%H:%M:%S')}] {s}", flush=True)

def api(path):
    last=None
    for k in range(RETRIES):
        try:
            req=urllib.request.Request(BASE+path,headers={'User-Agent':'tst-round6-edge-scanner/1.0'})
            with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
        except Exception as e:
            last=e; time.sleep(min(4.0,0.5*(2**k)))
    raise RuntimeError(f'API failed: {path}: {last}')

def universe():
    info=api('/api/v3/exchangeInfo'); tick={x['symbol']:x for x in api('/api/v3/ticker/24hr')}; rows=[]
    for s in info.get('symbols',[]):
        b=s.get('baseAsset',''); sym=s.get('symbol','')
        if s.get('status')!='TRADING' or s.get('quoteAsset')!='USDT' or not s.get('isSpotTradingAllowed'): continue
        if not b or b in EXCLUDED or b.endswith(('UP','DOWN','BULL','BEAR')): continue
        qv=float(tick.get(sym,{}).get('quoteVolume') or 0)
        if qv>=MIN_QV: rows.append((sym,qv))
    rows.sort(key=lambda x:x[1],reverse=True)
    return [s for s,_ in rows[:MAX_SYMBOLS]]

def klines(sym):
    end=int(time.time()*1000); cur=end-DAYS*86400000; rows=[]
    while cur<end:
        q=urllib.parse.urlencode({'symbol':sym,'interval':INTERVAL,'limit':1000,'startTime':cur,'endTime':end})
        b=api('/api/v3/klines?'+q)
        if not b: break
        rows.extend(b); nxt=int(b[-1][0])+3600000
        if nxt<=cur: break
        cur=nxt
    if len(rows)<4000: raise RuntimeError(f'{sym}: insufficient bars {len(rows)}')
    df=pd.DataFrame(rows,columns=['ot','open','high','low','close','volume','ct','quote_volume','trades','tb','tq','ignore'])
    for c in ['open','high','low','close','volume','quote_volume']: df[c]=pd.to_numeric(df[c],errors='coerce')
    df['ts']=pd.to_datetime(df.ot,unit='ms',utc=True)
    return df.set_index('ts')[['open','high','low','close','volume','quote_volume']].dropna()

def load_all(symbols):
    data={}; fail={}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        fut={ex.submit(klines,s):s for s in symbols}
        for n,f in enumerate(as_completed(fut),1):
            s=fut[f]
            try:data[s]=f.result(); log(f'DATA {n}/{len(symbols)} {s} {len(data[s])} bars')
            except Exception as e:fail[s]=str(e); log(f'DATA {n}/{len(symbols)} {s} FAIL {e}')
    return data,fail

def decluster(ix):
    out=[]; last=-10**18
    for i in sorted(ix):
        if i-last>=MIN_GAP: out.append(i); last=i
    return out

def pmean(a):
    if len(a)<2:return 1.0
    sd=float(a.std(ddof=1))
    if sd<=0:return 0.0 if float(a.mean())>0 else 1.0
    z=float(a.mean())/(sd/math.sqrt(len(a)))
    return 0.5*math.erfc(z/math.sqrt(2))

def event_stats(events,h):
    vals=[]; mfe=[]; mae=[]
    for df,i,sign in events:
        if i+h>=len(df): continue
        e=float(df.close.iloc[i]); f=float(df.close.iloc[i+h]); r=(f/e-1)*sign; vals.append(r)
        hi=float(df.high.iloc[i+1:i+h+1].max())/e-1
        lo=1-float(df.low.iloc[i+1:i+h+1].min())/e
        if sign>0: mfe.append(hi); mae.append(max(0.0,lo))
        else: mfe.append(lo); mae.append(max(0.0,hi))
    if not vals:return None
    a=np.asarray(vals,float); m=np.asarray(mfe,float); d=np.asarray(mae,float)
    med_mae=float(np.median(d)); ratio=float(np.median(m))/med_mae if med_mae>0 else float('inf')
    mean=float(a.mean()); med=float(np.median(a)); hit=float((a>0).mean())
    return {'n':len(a),'mean':mean,'median':med,'hitRate':hit,'medianMFE':float(np.median(m)),'medianMAE':med_mae,'mfeMaeRatio':ratio,'pMeanPositive':pmean(a),'rawPass':bool(len(a)>=MIN_EVENTS and mean>=0.015 and med>0 and hit>0.55 and ratio>=2)}

def bh(rows):
    ps=sorted([(i,r['pMeanPositive']) for i,r in enumerate(rows)],key=lambda x:x[1]); m=len(ps); adj=[1.0]*len(rows); run=1.0
    for rank in range(m,0,-1):
        i,p=ps[rank-1]; run=min(run,p*m/rank); adj[i]=min(1.0,run)
    for i,r in enumerate(rows): r['qValue']=adj[i]; r['pass']=bool(r['rawPass'] and adj[i]<=FDR_Q)

def main():
    t0=time.time(); syms=universe(); log(f'Universe={len(syms)}')
    data,fail=load_all(list(dict.fromkeys(['BTCUSDT']+syms)))
    if 'BTCUSDT' not in data: raise RuntimeError('BTC history unavailable')
    loaded=[s for s in syms if s in data and s!='BTCUSDT']
    common=data['BTCUSDT'].index
    for s in loaded: common=common.intersection(data[s].index)
    if len(common)<4000: raise RuntimeError(f'Common panel too short: {len(common)}')
    panel={s:data[s].reindex(common) for s in loaded}; btc=data['BTCUSDT'].reindex(common)
    close=pd.DataFrame({s:panel[s].close for s in loaded},index=common)
    qvol=pd.DataFrame({s:panel[s].quote_volume for s in loaded},index=common)
    ret1=close.pct_change(); btc1=btc.close.pct_change()
    pools={
      'BTC_SHOCK_ALT_UNDERREACTION_V1:UP':[], 'BTC_SHOCK_ALT_UNDERREACTION_V1:DOWN':[],
      'RESIDUAL_EXTREME_REVERSAL_V1:NEG':[], 'RESIDUAL_EXTREME_REVERSAL_V1:POS':[],
      'VOLATILITY_REGIME_EXPANSION_V1:UP':[], 'VOLATILITY_REGIME_EXPANSION_V1:DOWN':[],
      'VOLUME_SHOCK_V1:UP_CONT':[], 'VOLUME_SHOCK_V1:DOWN_REV':[]}

    # 1) BTC shock + alt underreaction. Direction follows BTC shock.
    btc6=btc.close.pct_change(BTC_SHOCK_H)
    for s in loaded:
        df=panel[s]; a6=df.close.pct_change(BTC_SHOCK_H)
        up=(btc6>=BTC_SHOCK_ABS)&(a6>=0)&(a6<=btc6*UNDERREACTION_RATIO)
        dn=(btc6<=-BTC_SHOCK_ABS)&(a6<=0)&(a6>=btc6*UNDERREACTION_RATIO)
        pools['BTC_SHOCK_ALT_UNDERREACTION_V1:UP'] += [(df,i,1) for i in decluster(np.flatnonzero(up.fillna(False).to_numpy()).tolist())]
        pools['BTC_SHOCK_ALT_UNDERREACTION_V1:DOWN'] += [(df,i,-1) for i in decluster(np.flatnonzero(dn.fillna(False).to_numpy()).tolist())]

    # 2) Rolling beta residual 24h extremes, test reversal toward zero.
    btc24=btc.close.pct_change(RESID_H)
    for s in loaded:
        df=panel[s]; r24=df.close.pct_change(RESID_H)
        cov=ret1[s].rolling(BETA_LOOKBACK).cov(btc1); var=btc1.rolling(BETA_LOOKBACK).var().replace(0,np.nan)
        beta=cov/var; resid=r24-beta*btc24
        mu=resid.rolling(RESID_Z_LOOKBACK).mean(); sd=resid.rolling(RESID_Z_LOOKBACK).std(ddof=0).replace(0,np.nan); z=(resid-mu)/sd
        neg=decluster(np.flatnonzero((z<=-RESID_Z).fillna(False).to_numpy()).tolist())
        pos=decluster(np.flatnonzero((z>= RESID_Z).fillna(False).to_numpy()).tolist())
        pools['RESIDUAL_EXTREME_REVERSAL_V1:NEG'] += [(df,i,1) for i in neg]
        pools['RESIDUAL_EXTREME_REVERSAL_V1:POS'] += [(df,i,-1) for i in pos]

    # 3) Realized-vol regime expansion after low-vol state, direction from 6h return.
    for s in loaded:
        df=panel[s]; r=df.close.pct_change()
        vshort=r.rolling(VOL_SHORT).std(ddof=0); vbase=r.rolling(VOL_BASE).std(ddof=0)
        low_thr=vbase.rolling(VOL_Q_LOOKBACK).quantile(VOL_LOW_Q)
        prior_low=vbase.shift(1)<=low_thr.shift(1)
        expand=vshort>=vbase*VOL_EXPAND_MULT; r6=df.close.pct_change(VOL_SHORT)
        up=prior_low&expand&(r6>=VOL_DIR_RET); dn=prior_low&expand&(r6<=-VOL_DIR_RET)
        pools['VOLATILITY_REGIME_EXPANSION_V1:UP'] += [(df,i,1) for i in decluster(np.flatnonzero(up.fillna(False).to_numpy()).tolist())]
        pools['VOLATILITY_REGIME_EXPANSION_V1:DOWN'] += [(df,i,-1) for i in decluster(np.flatnonzero(dn.fillna(False).to_numpy()).tolist())]

    # 4) Quote-volume shock. Positive shock tests continuation; negative shock tests reversal.
    for s in loaded:
        df=panel[s]; medq=df.quote_volume.rolling(QV_LOOKBACK).median().replace(0,np.nan); mult=df.quote_volume/medq
        r6=df.close.pct_change(SHOCK_RET_H)
        up=(mult>=QV_MULT)&(r6>=SHOCK_RET); dn=(mult>=QV_MULT)&(r6<=-SHOCK_RET)
        pools['VOLUME_SHOCK_V1:UP_CONT'] += [(df,i,1) for i in decluster(np.flatnonzero(up.fillna(False).to_numpy()).tolist())]
        pools['VOLUME_SHOCK_V1:DOWN_REV'] += [(df,i,1) for i in decluster(np.flatnonzero(dn.fillna(False).to_numpy()).tolist())]

    tests=[]
    for family,ev in pools.items():
        log(f'{family}: events={len(ev)}')
        for h in HORIZONS:
            st=event_stats(ev,h)
            if st: tests.append({'family':family,'horizon':h,**st})
    bh(tests)
    survivors=sorted([r for r in tests if r['pass']],key=lambda r:(r['qValue'],-r['mean'],-r['n']))
    report={
      'engine':'EVENT_DRIVEN_EDGE_SCANNER_V2','round':6,'authorization':'RESEARCH_ONLY','liveTrading':False,
      'interval':INTERVAL,'days':DAYS,
      'families':['BTC_SHOCK_ALT_UNDERREACTION_V1','RESIDUAL_EXTREME_REVERSAL_V1','VOLATILITY_REGIME_EXPANSION_V1','VOLUME_SHOCK_CONTINUATION_REVERSAL_V1'],
      'frozenDefinitions':{
        'BTC_SHOCK_ALT_UNDERREACTION_V1':{'btcReturnHours':BTC_SHOCK_H,'btcAbsShock':BTC_SHOCK_ABS,'underreactionRatio':UNDERREACTION_RATIO},
        'RESIDUAL_EXTREME_REVERSAL_V1':{'returnHours':RESID_H,'betaLookbackHours':BETA_LOOKBACK,'zLookbackHours':RESID_Z_LOOKBACK,'absZ':RESID_Z},
        'VOLATILITY_REGIME_EXPANSION_V1':{'shortVolHours':VOL_SHORT,'baseVolHours':VOL_BASE,'lowVolQuantile':VOL_LOW_Q,'quantileLookbackHours':VOL_Q_LOOKBACK,'expansionMultiple':VOL_EXPAND_MULT,'directionalReturn':VOL_DIR_RET},
        'VOLUME_SHOCK_CONTINUATION_REVERSAL_V1':{'quoteVolumeLookbackHours':QV_LOOKBACK,'quoteVolumeMultiple':QV_MULT,'returnHours':SHOCK_RET_H,'absReturn':SHOCK_RET}},
      'rawGate':{'minEvents':MIN_EVENTS,'mean':0.015,'median':'>0','hitRate':'>0.55','mfeMaeRatio':2.0},
      'multipleTesting':{'method':'Benjamini-Hochberg','q':FDR_Q},
      'symbolsRequested':syms,'symbolsLoaded':loaded,'failures':fail,'eventCounts':{k:len(v) for k,v in pools.items()},
      'tests':len(tests),'results':tests,'survivorCount':len(survivors),'survivors':survivors,
      'runtimeSeconds':round(time.time()-t0,1)}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(report,indent=2,sort_keys=True)); print(json.dumps(report,indent=2,sort_keys=True))

if __name__=='__main__': main()
