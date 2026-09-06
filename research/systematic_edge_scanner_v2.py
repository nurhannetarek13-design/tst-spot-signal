#!/usr/bin/env python3
"""Systematic raw-edge scanner v2.

Fixes v1 coverage flaws without tuning edge thresholds:
- broader liquid USDT universe (no price cap, no max-volume cap, majors included)
- pooled cross-sectional tests in addition to per-symbol tests
- bounded concurrent Binance history loading with retries and explicit progress
- same raw gates and BH-FDR family control
Research only; never authorizes live trading.
"""
from __future__ import annotations

import json, math, pathlib, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import product
import numpy as np
import pandas as pd

BASE_URL='https://data-api.binance.vision'
INTERVAL='1h'
DAYS=365
MAX_SYMBOLS=80
MIN_QV=10_000_000
HORIZONS=(24,48,72)
MIN_EVENTS=40
MIN_GAP=24
FDR_Q=0.05
DOWNLOAD_WORKERS=8
API_RETRIES=4
OUT=pathlib.Path('validation/edges/systematic-edge-scanner-v2-latest.json')
EXCLUDED={'USDC','FDUSD','TUSD','USDP','DAI','BUSD','EUR','AEUR','TRY','BRL','GBP','AUD','USD1','RLUSD','USDE','PAXG','XAUT'}
FEATURES=('rvol26','taker_buy_ratio','rel_strength_6h','rel_strength_24h','vol_ratio_24_168','ret_6h','ret_24h','drawdown_72h')

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def api(path):
    last=None
    for attempt in range(1,API_RETRIES+1):
        try:
            req=urllib.request.Request(BASE_URL+path,headers={'User-Agent':'tst-systematic-edge-scanner-v2/1.1'})
            with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
        except Exception as e:
            last=e
            if attempt==API_RETRIES: break
            time.sleep(min(4.0,0.5*(2**(attempt-1))))
    raise RuntimeError(f'API failed after {API_RETRIES} attempts: {path}: {last}')

def universe():
    info=api('/api/v3/exchangeInfo'); ticker={x['symbol']:x for x in api('/api/v3/ticker/24hr')}; rows=[]
    for s in info.get('symbols',[]):
        base=s.get('baseAsset','')
        if s.get('status')!='TRADING' or s.get('quoteAsset')!='USDT' or not s.get('isSpotTradingAllowed'): continue
        if not base or base in EXCLUDED or base.endswith(('UP','DOWN','BULL','BEAR')): continue
        qv=float(ticker.get(s['symbol'],{}).get('quoteVolume') or 0)
        if qv>=MIN_QV: rows.append((s['symbol'],qv))
    rows.sort(key=lambda x:x[1],reverse=True)
    return [s for s,_ in rows[:MAX_SYMBOLS]]

def klines(symbol):
    end=int(time.time()*1000); cur=end-DAYS*86400000; rows=[]
    pages=0
    while cur<end:
        q=urllib.parse.urlencode({'symbol':symbol,'interval':INTERVAL,'limit':1000,'startTime':cur,'endTime':end})
        b=api('/api/v3/klines?'+q)
        if not b: break
        rows.extend(b); pages+=1; nxt=int(b[-1][0])+3600000
        if nxt<=cur: break
        cur=nxt
    if len(rows)<4000: raise RuntimeError(f'{symbol}: insufficient bars {len(rows)}')
    df=pd.DataFrame(rows,columns=['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_base','taker_quote','ignore'])
    for c in ['open','high','low','close','volume','quote_volume','taker_quote']: df[c]=pd.to_numeric(df[c],errors='coerce')
    df['ts']=pd.to_datetime(df.open_time,unit='ms',utc=True)
    return df.set_index('ts')[['open','high','low','close','volume','quote_volume','taker_quote']].dropna(), pages

def features(df,btc_close):
    c=df.close; btc=btc_close.reindex(df.index).ffill(); r1=c.pct_change(); rv24=r1.rolling(24).std(ddof=0); rv168=r1.rolling(168).std(ddof=0)
    out=pd.DataFrame(index=df.index)
    out['rvol26']=df.volume/df.volume.rolling(26).mean().replace(0,np.nan)
    out['taker_buy_ratio']=df.taker_quote/df.quote_volume.replace(0,np.nan)
    out['rel_strength_6h']=c.pct_change(6)-btc.pct_change(6)
    out['rel_strength_24h']=c.pct_change(24)-btc.pct_change(24)
    out['vol_ratio_24_168']=rv24/rv168.replace(0,np.nan)
    out['ret_6h']=c.pct_change(6); out['ret_24h']=c.pct_change(24)
    out['drawdown_72h']=c/c.rolling(72).max()-1
    return out.replace([np.inf,-np.inf],np.nan)

def qbucket(s,q):
    try:return pd.qcut(s,q=q,labels=False,duplicates='drop')
    except ValueError:return pd.Series(np.nan,index=s.index)

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

def stats(df,ix,h):
    vals=[]; mf=[]; ma=[]
    for i in decluster(ix):
        if i+h>=len(df): continue
        e=float(df.close.iloc[i]); vals.append(float(df.close.iloc[i+h])/e-1)
        mf.append(float(df.high.iloc[i+1:i+h+1].max())/e-1)
        ma.append(max(0.0,1-float(df.low.iloc[i+1:i+h+1].min())/e))
    if not vals:return None
    a=np.asarray(vals,float); m=np.asarray(mf,float); d=np.asarray(ma,float); med_mae=float(np.median(d))
    ratio=float(np.median(m))/med_mae if med_mae>0 else (float('inf') if float(np.median(m))>0 else 0.0)
    mean=float(a.mean()); med=float(np.median(a)); hit=float((a>0).mean())
    return {'n':len(a),'mean':mean,'median':med,'hitRate':hit,'medianMFE':float(np.median(m)),'medianMAE':med_mae,'mfeMaeRatio':ratio,'pMeanPositive':pmean(a),'rawPass':bool(len(a)>=MIN_EVENTS and mean>=0.015 and med>0 and hit>0.55 and ratio>=2)}

def bh(rows):
    ps=sorted([(i,r['pMeanPositive']) for i,r in enumerate(rows)],key=lambda x:x[1]); m=len(ps); adj=[1.0]*len(rows); run=1.0
    for rank in range(m,0,-1):
        i,p=ps[rank-1]; run=min(run,p*m/rank); adj[i]=min(1.0,run)
    for i,r in enumerate(rows): r['qValue']=adj[i]; r['pass']=bool(r['rawPass'] and adj[i]<=FDR_Q)

def pooled_stats(events,h):
    vals=[]; mf=[]; ma=[]
    for df,i in events:
        if i+h>=len(df): continue
        e=float(df.close.iloc[i]); vals.append(float(df.close.iloc[i+h])/e-1)
        mf.append(float(df.high.iloc[i+1:i+h+1].max())/e-1); ma.append(max(0.0,1-float(df.low.iloc[i+1:i+h+1].min())/e))
    if not vals:return None
    a=np.asarray(vals); m=np.asarray(mf); d=np.asarray(ma); med_mae=float(np.median(d)); ratio=float(np.median(m))/med_mae if med_mae>0 else float('inf')
    mean=float(a.mean()); med=float(np.median(a)); hit=float((a>0).mean())
    return {'n':len(a),'mean':mean,'median':med,'hitRate':hit,'medianMFE':float(np.median(m)),'medianMAE':med_mae,'mfeMaeRatio':ratio,'pMeanPositive':pmean(a),'rawPass':bool(len(a)>=MIN_EVENTS and mean>=0.015 and med>0 and hit>0.55 and ratio>=2)}

def load_history(symbols):
    data={}; fail={}; total=len(symbols)
    log(f'Loading {total} symbols with {DOWNLOAD_WORKERS} workers')
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as ex:
        fut={ex.submit(klines,s):s for s in symbols}
        done=0
        for f in as_completed(fut):
            s=fut[f]; done+=1
            try:
                df,pages=f.result(); data[s]=df
                log(f'DATA {done}/{total} {s}: {len(df)} bars, {pages} pages')
            except Exception as e:
                fail[s]=str(e); log(f'DATA {done}/{total} {s}: FAIL {e}')
    return data,fail

def main():
    started=time.time()
    syms=universe(); log(f'Universe selected: {len(syms)} symbols')
    data,fail=load_history(['BTCUSDT']+syms)
    if 'BTCUSDT' not in data: raise RuntimeError('BTCUSDT history unavailable')
    log(f'History complete: loaded={len(data)-1}/{len(syms)} failures={len(fail)}')
    tests=[]; pooled={}; loaded=[s for s in syms if s in data]
    for pos,s in enumerate(loaded,1):
        df=data[s]; ft=features(df,data['BTCUSDT'].close); qb={f:qbucket(ft[f],5) for f in FEATURES}
        for f in FEATURES:
            for q in range(5):
                ix=np.flatnonzero((qb[f]==q).fillna(False).to_numpy()).tolist()
                pooled.setdefault(('u',f,q),[]).extend((df,i) for i in decluster(ix))
                for h in HORIZONS:
                    st=stats(df,ix,h)
                    if st: tests.append({'scope':'symbol','symbol':s,'kind':'univariate_quintile','feature':f,'bucket':q,'horizon':h,**st})
        core=['rvol26','taker_buy_ratio','rel_strength_6h']; tb={f:qbucket(ft[f],3) for f in core}
        for comb in product(range(3),repeat=3):
            mask=((tb[core[0]]==comb[0])&(tb[core[1]]==comb[1])&(tb[core[2]]==comb[2])).fillna(False)
            ix=np.flatnonzero(mask.to_numpy()).tolist(); pooled.setdefault(('c',)+comb,[]).extend((df,i) for i in decluster(ix))
        if pos==1 or pos%5==0 or pos==len(loaded): log(f'FEATURES {pos}/{len(loaded)} complete; symbol-tests={len(tests)}')
    log(f'Building pooled tests across {len(pooled)} predeclared buckets')
    for n,(key,ev) in enumerate(pooled.items(),1):
        for h in HORIZONS:
            st=pooled_stats(ev,h)
            if not st: continue
            if key[0]=='u': tests.append({'scope':'pooled','kind':'univariate_quintile','feature':key[1],'bucket':key[2],'horizon':h,**st})
            else: tests.append({'scope':'pooled','kind':'core_3way_tercile','buckets':{'rvol26':key[1],'taker_buy_ratio':key[2],'rel_strength_6h':key[3]},'horizon':h,**st})
        if n%20==0 or n==len(pooled): log(f'POOLED {n}/{len(pooled)} complete; total-tests={len(tests)}')
    log(f'Applying BH-FDR across {len(tests)} tests')
    bh(tests); surv=sorted([r for r in tests if r['pass']],key=lambda r:(r['qValue'],-r['mean'],-r['n']))
    report={'engine':'SYSTEMATIC_EDGE_SCANNER_V2','engineRevision':'1.1-parallel-loader','authorization':'RESEARCH_ONLY','liveTrading':False,'interval':INTERVAL,'days':DAYS,'universePolicy':{'maxSymbols':MAX_SYMBOLS,'minCurrent24hQuoteVolume':MIN_QV,'priceCap':None,'maxQuoteVolume':None,'majorsIncluded':True},'dataLoading':{'workers':DOWNLOAD_WORKERS,'apiRetries':API_RETRIES},'multipleTesting':{'method':'Benjamini-Hochberg','q':FDR_Q},'rawGate':{'minEvents':MIN_EVENTS,'mean':0.015,'median':'>0','hitRate':'>0.55','mfeMaeRatio':2.0},'symbolsRequested':syms,'symbolsLoaded':loaded,'failures':fail,'tests':len(tests),'survivorCount':len(surv),'survivors':surv,'runtimeSeconds':round(time.time()-started,2)}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(report,indent=2,sort_keys=True,allow_nan=False)); log(f"DONE symbolsLoaded={len(loaded)} tests={len(tests)} survivors={len(surv)} runtime={report['runtimeSeconds']}s out={OUT}")
if __name__=='__main__': main()
