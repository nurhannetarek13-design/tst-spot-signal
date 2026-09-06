#!/usr/bin/env python3
"""Round 5 event-driven raw-edge discovery.

Predeclared event families (fixed before results):
1) CAPITULATION_CLUSTER_V1
2) SQUEEZE_RELEASE_V1
3) BREADTH_THRUST_AFTER_WASHOUT_V1
4) LEADER_LAGGARD_DISLOCATION_V1

Discovery only. No strategy rules, costs, sizing, optimization, or live authorization.
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
OUT=pathlib.Path('validation/edges/event-driven-edge-scanner-v1-latest.json')
EXCLUDED={'USDC','FDUSD','TUSD','USDP','DAI','BUSD','EUR','AEUR','TRY','BRL','GBP','AUD','USD1','RLUSD','USDE','PAXG','XAUT'}

# Frozen V1 definitions. These are not modified after seeing results.
CAP_VOL_LOOKBACK=150; CAP_VOL_MULT=5.0; CAP_LOW_LOOKBACK=150; CAP_ATR_LOOKBACK=14; CAP_RANGE_ATR=2.0
SQ_LEN=20; SQ_BB_MULT=2.0; SQ_KC_MULT=1.5
BREADTH_RET_H=24; BREADTH_SMOOTH=24; BREADTH_WASHOUT_LOOKBACK=240; BREADTH_LOW=0.40; BREADTH_HIGH=0.615
DISLOC_RET_H=24; DISLOC_Z=2.5


def log(s): print(f"[{time.strftime('%H:%M:%S')}] {s}", flush=True)

def api(path):
    last=None
    for k in range(RETRIES):
        try:
            req=urllib.request.Request(BASE+path,headers={'User-Agent':'tst-event-edge-scanner/1.0'})
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

def atr(df,n):
    pc=df.close.shift(1)
    tr=pd.concat([(df.high-df.low),(df.high-pc).abs(),(df.low-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()

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
    for df,i in events:
        if i+h>=len(df): continue
        e=float(df.close.iloc[i]); vals.append(float(df.close.iloc[i+h])/e-1)
        mfe.append(float(df.high.iloc[i+1:i+h+1].max())/e-1)
        mae.append(max(0.0,1-float(df.low.iloc[i+1:i+h+1].min())/e))
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
    panel={s:data[s].reindex(common) for s in loaded}
    close=pd.DataFrame({s:panel[s].close for s in loaded},index=common)
    qvol=pd.DataFrame({s:panel[s].quote_volume for s in loaded},index=common)
    btc=data['BTCUSDT'].close.reindex(common)
    pools={
        'CAPITULATION_CLUSTER_V1':[], 'SQUEEZE_RELEASE_V1':[],
        'BREADTH_THRUST_AFTER_WASHOUT_V1':[],
        'LEADER_LAGGARD_DISLOCATION_V1:LAGGARD':[],
        'LEADER_LAGGARD_DISLOCATION_V1:LEADER':[]}

    # 1) Capitulation: abnormal volume + local low + oversized bearish range.
    for n,s in enumerate(loaded,1):
        df=panel[s]
        vol_sma=df.volume.rolling(CAP_VOL_LOOKBACK).mean()
        low_roll=df.low.rolling(CAP_LOW_LOOKBACK).min()
        a=atr(df,CAP_ATR_LOOKBACK)
        rng=df.high-df.low
        sig=(df.volume>=vol_sma*CAP_VOL_MULT)&(df.low<=low_roll)&(rng>=a*CAP_RANGE_ATR)&(df.close<df.open)
        ix=decluster(np.flatnonzero(sig.fillna(False).to_numpy()).tolist())
        pools['CAPITULATION_CLUSTER_V1'].extend((df,i) for i in ix)

        # 2) TTM-style squeeze release, confirmed at bar close, bullish momentum only.
        mid=df.close.rolling(SQ_LEN).mean(); sd=df.close.rolling(SQ_LEN).std(ddof=0)
        bb_up=mid+SQ_BB_MULT*sd; bb_dn=mid-SQ_BB_MULT*sd
        tr=pd.concat([(df.high-df.low),(df.high-df.close.shift()).abs(),(df.low-df.close.shift()).abs()],axis=1).max(axis=1)
        ema_mid=df.close.ewm(span=SQ_LEN,adjust=False).mean(); ema_tr=tr.ewm(span=SQ_LEN,adjust=False).mean()
        kc_up=ema_mid+SQ_KC_MULT*ema_tr; kc_dn=ema_mid-SQ_KC_MULT*ema_tr
        squeeze=(bb_up<kc_up)&(bb_dn>kc_dn); release=squeeze.shift(1).fillna(False)&(~squeeze)
        momentum=(df.close-df.close.rolling(SQ_LEN).mean()); bull=momentum>momentum.shift(1)
        ix=decluster(np.flatnonzero((release&bull).fillna(False).to_numpy()).tolist())
        pools['SQUEEZE_RELEASE_V1'].extend((df,i) for i in ix)
        if n%10==0 or n==len(loaded): log(f'SYMBOL EVENTS {n}/{len(loaded)}')

    # 3) Crypto-native breadth thrust: breadth + advancing quote-volume share.
    ret24=close.pct_change(BREADTH_RET_H)
    adv=(ret24>0)
    breadth=adv.mean(axis=1)
    adv_qv=qvol.where(adv,0).sum(axis=1); total_qv=qvol.sum(axis=1).replace(0,np.nan)
    vol_breadth=adv_qv/total_qv
    combined=((breadth+vol_breadth)/2).rolling(BREADTH_SMOOTH).mean()
    prior_low=combined.shift(1).rolling(BREADTH_WASHOUT_LOOKBACK).min()
    thrust=(prior_low<BREADTH_LOW)&(combined>BREADTH_HIGH)&(combined.shift(1)<=BREADTH_HIGH)
    tix=decluster(np.flatnonzero(thrust.fillna(False).to_numpy()).tolist())
    for s in loaded:
        df=panel[s]; pools['BREADTH_THRUST_AFTER_WASHOUT_V1'].extend((df,i) for i in tix)

    # 4) Cross-sectional relative dislocation vs BTC. Test both frozen tails; FDR covers both.
    btc24=btc.pct_change(DISLOC_RET_H)
    rel=ret24.sub(btc24,axis=0)
    mu=rel.mean(axis=1); sd=rel.std(axis=1,ddof=0).replace(0,np.nan)
    z=rel.sub(mu,axis=0).div(sd,axis=0)
    for s in loaded:
        df=panel[s]
        lag=decluster(np.flatnonzero((z[s]<=-DISLOC_Z).fillna(False).to_numpy()).tolist())
        lead=decluster(np.flatnonzero((z[s]>= DISLOC_Z).fillna(False).to_numpy()).tolist())
        pools['LEADER_LAGGARD_DISLOCATION_V1:LAGGARD'].extend((df,i) for i in lag)
        pools['LEADER_LAGGARD_DISLOCATION_V1:LEADER'].extend((df,i) for i in lead)

    tests=[]
    for family,ev in pools.items():
        log(f'{family}: events={len(ev)}')
        for h in HORIZONS:
            st=event_stats(ev,h)
            if st: tests.append({'family':family,'horizon':h,**st})
    bh(tests)
    survivors=sorted([r for r in tests if r['pass']],key=lambda r:(r['qValue'],-r['mean'],-r['n']))
    report={
      'engine':'EVENT_DRIVEN_EDGE_SCANNER_V1','round':5,'authorization':'RESEARCH_ONLY','liveTrading':False,
      'interval':INTERVAL,'days':DAYS,
      'families':['CAPITULATION_CLUSTER_V1','SQUEEZE_RELEASE_V1','BREADTH_THRUST_AFTER_WASHOUT_V1','LEADER_LAGGARD_DISLOCATION_V1'],
      'frozenDefinitions':{
        'CAPITULATION_CLUSTER_V1':{'volumeLookback':CAP_VOL_LOOKBACK,'volumeMultiplier':CAP_VOL_MULT,'lowestLowLookback':CAP_LOW_LOOKBACK,'atrLookback':CAP_ATR_LOOKBACK,'rangeAtrMultiplier':CAP_RANGE_ATR,'bearishCandle':True},
        'SQUEEZE_RELEASE_V1':{'length':SQ_LEN,'bbMultiplier':SQ_BB_MULT,'kcMultiplier':SQ_KC_MULT,'release':'prior squeeze -> no squeeze','momentum':'positive slope'},
        'BREADTH_THRUST_AFTER_WASHOUT_V1':{'returnHours':BREADTH_RET_H,'smoothHours':BREADTH_SMOOTH,'washoutLookbackHours':BREADTH_WASHOUT_LOOKBACK,'low':BREADTH_LOW,'high':BREADTH_HIGH,'combined':'equal-weight advance breadth + advancing quote-volume breadth'},
        'LEADER_LAGGARD_DISLOCATION_V1':{'relativeReturnHours':DISLOC_RET_H,'crossSectionalZ':DISLOC_Z,'tails':['laggard','leader'],'benchmark':'BTCUSDT'}},
      'rawGate':{'minEvents':MIN_EVENTS,'mean':0.015,'median':'>0','hitRate':'>0.55','mfeMaeRatio':2.0},
      'multipleTesting':{'method':'Benjamini-Hochberg','q':FDR_Q},
      'symbolsRequested':syms,'symbolsLoaded':loaded,'failures':fail,'eventCounts':{k:len(v) for k,v in pools.items()},
      'tests':len(tests),'results':tests,'survivorCount':len(survivors),'survivors':survivors,'runtimeSeconds':round(time.time()-t0,2)}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(report,indent=2,sort_keys=True,allow_nan=False))
    log(f"DONE loaded={len(loaded)} tests={len(tests)} survivors={len(survivors)} runtime={report['runtimeSeconds']}s")

if __name__=='__main__': main()
