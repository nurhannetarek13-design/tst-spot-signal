#!/usr/bin/env python3
"""Orthogonal raw-edge discovery after RVOL/orderflow/RS rejection.

Predeclared families:
1) market-wide state (breadth, dispersion, median return)
2) BTC-beta residual dislocation
3) liquidity shock proxy (quote-volume shock + Amihud shock)
4) volatility term structure

Discovery only. No strategy rules, no costs, no sizing, no live authorization.
"""
from __future__ import annotations

import json, math, pathlib, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import product
import numpy as np
import pandas as pd

BASE='https://data-api.binance.vision'
INTERVAL='1h'; DAYS=365; MAX_SYMBOLS=60; MIN_QV=10_000_000
HORIZONS=(24,48,72); MIN_EVENTS=40; MIN_GAP=24; FDR_Q=0.05
WORKERS=8; RETRIES=4
OUT=pathlib.Path('validation/edges/orthogonal-edge-scanner-v1-latest.json')
EXCLUDED={'USDC','FDUSD','TUSD','USDP','DAI','BUSD','EUR','AEUR','TRY','BRL','GBP','AUD','USD1','RLUSD','USDE','PAXG','XAUT'}


def log(s): print(f"[{time.strftime('%H:%M:%S')}] {s}",flush=True)

def api(path):
    last=None
    for k in range(RETRIES):
        try:
            req=urllib.request.Request(BASE+path,headers={'User-Agent':'tst-orthogonal-edge-scanner/1.0'})
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

def qbucket(s,q=5):
    try:return pd.qcut(s,q=q,labels=False,duplicates='drop')
    except Exception:return pd.Series(np.nan,index=s.index)

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
    close=pd.DataFrame({s:data[s].close.reindex(common) for s in loaded})
    qvol=pd.DataFrame({s:data[s].quote_volume.reindex(common) for s in loaded})
    btc=data['BTCUSDT'].close.reindex(common)
    r1=close.pct_change(); r24=close.pct_change(24); btc1=btc.pct_change(); btc24=btc.pct_change(24)

    # Market-wide state, shared across symbols at each timestamp.
    market=pd.DataFrame(index=common)
    market['breadth24']=(r24>0).mean(axis=1)
    market['dispersion24']=r24.std(axis=1,ddof=0)
    market['medianRet24']=r24.median(axis=1)
    market['breadthMomentum24']=market.breadth24-market.breadth24.shift(24)

    tests=[]; pools={}
    # Predeclared market-state quintiles pooled across symbols.
    for f in market.columns:
        qb=qbucket(market[f],5)
        for q in range(5):
            times=common[(qb==q).fillna(False)]
            key=('market',f,q); ev=[]
            for s in loaded:
                df=data[s].reindex(common); idx=np.flatnonzero(common.isin(times)).tolist()
                ev.extend((df,i) for i in decluster(idx))
            pools[key]=ev

    # Symbol-specific orthogonal features then pooled by bucket.
    for pos,s in enumerate(loaded,1):
        df=data[s].reindex(common); c=close[s]; rv1=r1[s]
        beta=rv1.rolling(168).cov(btc1)/btc1.rolling(168).var().replace(0,np.nan)
        residual24=r24[s]-beta*btc24
        residual_z=(residual24-residual24.rolling(168).mean())/residual24.rolling(168).std(ddof=0).replace(0,np.nan)
        amihud=rv1.abs()/qvol[s].replace(0,np.nan)
        amihud_shock=amihud/amihud.rolling(168).median().replace(0,np.nan)
        qv_shock=qvol[s]/qvol[s].rolling(168).median().replace(0,np.nan)
        vol24=rv1.rolling(24).std(ddof=0); vol168=rv1.rolling(168).std(ddof=0)
        feats=pd.DataFrame({'residual24':residual24,'residualZ168':residual_z,'beta168':beta,'amihudShock':amihud_shock,'quoteVolumeShock':qv_shock,'volTerm24_168':vol24/vol168.replace(0,np.nan)},index=common)
        for f in feats:
            qb=qbucket(feats[f],5)
            for q in range(5):
                ix=np.flatnonzero((qb==q).fillna(False).to_numpy()).tolist()
                pools.setdefault(('symbol',f,q),[]).extend((df,i) for i in decluster(ix))
        # Only two predeclared 2-way families to avoid combinatorial fishing.
        rz=qbucket(feats['residualZ168'],3); lq=qbucket(feats['amihudShock'],3); vt=qbucket(feats['volTerm24_168'],3)
        for a,b in product(range(3),repeat=2):
            ix=np.flatnonzero(((rz==a)&(lq==b)).fillna(False).to_numpy()).tolist()
            pools.setdefault(('pair','residualZ168','amihudShock',a,b),[]).extend((df,i) for i in decluster(ix))
            ix=np.flatnonzero(((rz==a)&(vt==b)).fillna(False).to_numpy()).tolist()
            pools.setdefault(('pair','residualZ168','volTerm24_168',a,b),[]).extend((df,i) for i in decluster(ix))
        if pos%5==0 or pos==len(loaded): log(f'FEATURES {pos}/{len(loaded)}')

    log(f'Predeclared pools={len(pools)}')
    for key,ev in pools.items():
        for h in HORIZONS:
            st=event_stats(ev,h)
            if st: tests.append({'family':key[0],'definition':list(key[1:]),'horizon':h,**st})
    log(f'Tests={len(tests)}; applying BH-FDR')
    bh(tests); survivors=sorted([r for r in tests if r['pass']],key=lambda r:(r['qValue'],-r['mean'],-r['n']))
    report={'engine':'ORTHOGONAL_EDGE_SCANNER_V1','authorization':'RESEARCH_ONLY','liveTrading':False,'interval':INTERVAL,'days':DAYS,'families':['market_state','btc_beta_residual','liquidity_shock_proxy','volatility_term_structure'],'rawGate':{'minEvents':MIN_EVENTS,'mean':0.015,'median':'>0','hitRate':'>0.55','mfeMaeRatio':2.0},'multipleTesting':{'method':'Benjamini-Hochberg','q':FDR_Q},'symbolsRequested':syms,'symbolsLoaded':loaded,'failures':fail,'tests':len(tests),'survivorCount':len(survivors),'survivors':survivors,'runtimeSeconds':round(time.time()-t0,2)}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(report,indent=2,sort_keys=True,allow_nan=False))
    log(f"DONE loaded={len(loaded)} tests={len(tests)} survivors={len(survivors)} runtime={report['runtimeSeconds']}s")

if __name__=='__main__': main()
