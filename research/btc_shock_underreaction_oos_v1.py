#!/usr/bin/env python3
"""Frozen temporal OOS for Round 6 survivor BTC_SHOCK_ALT_UNDERREACTION_V1:DOWN.

Discovery period was the most recent ~365d ending 2026-09-11.
This validator uses the disjoint preceding year: 2024-09-11 through 2025-09-10 UTC.
Candidate definition and gates are frozen exactly from Round 6.

Important: the symbol set is frozen from the Round 6 discovery universe, so this is
TEMPORAL_OOS_ONLY. It does not clear survivorship/current-liquidity bias.
"""
from __future__ import annotations
import json, math, pathlib, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import pandas as pd

BASE='https://data-api.binance.vision'
INTERVAL='1h'
START='2024-09-11T00:00:00Z'
END='2025-09-11T00:00:00Z'  # exclusive
BTC_SHOCK_H=6
BTC_SHOCK_ABS=0.03
UNDERREACTION_RATIO=0.50
MIN_GAP=24
HORIZONS=(48,72)
MIN_EVENTS=40
FDR_Q=0.05
WORKERS=8
RETRIES=4
OUT=pathlib.Path('validation/edges/btc-shock-underreaction-oos-v1-latest.json')

# Frozen from Round 6 symbolsLoaded, excluding BTC benchmark.
SYMBOLS=[
 'ETHUSDT','ZECUSDT','SOLUSDT','XRPUSDT','SAHARAUSDT','BNBUSDT','NEARUSDT','HOLOUSDT',
 'SUIUSDT','DOGEUSDT','UNIUSDT','ENAUSDT','ADAUSDT','RAYUSDT','PUMPUSDT','TRXUSDT',
 'TAOUSDT','LINKUSDT','PEPEUSDT','ETHFIUSDT','ARBUSDT','BCHUSDT','AVAXUSDT','THEUSDT',
 'WLDUSDT','WLFIUSDT','DASHUSDT','LTCUSDT','DOTUSDT','TRUMPUSDT','SAGAUSDT','ASTERUSDT',
 'XLMUSDT','VTHOUSDT','ONDOUSDT','METUSDT','APTUSDT','SOPHUSDT'
]

def ms(s): return int(pd.Timestamp(s).timestamp()*1000)
START_MS=ms(START); END_MS=ms(END)

def log(s): print(f"[{time.strftime('%H:%M:%S')}] {s}",flush=True)

def api(path):
    last=None
    for k in range(RETRIES):
        try:
            req=urllib.request.Request(BASE+path,headers={'User-Agent':'tst-btc-shock-oos/1.0'})
            with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
        except Exception as e:
            last=e; time.sleep(min(4.0,0.5*(2**k)))
    raise RuntimeError(f'API failed {path}: {last}')

def klines(sym):
    cur=START_MS; rows=[]
    while cur<END_MS:
        q=urllib.parse.urlencode({'symbol':sym,'interval':INTERVAL,'limit':1000,'startTime':cur,'endTime':END_MS-1})
        b=api('/api/v3/klines?'+q)
        if not b: break
        rows.extend(b); nxt=int(b[-1][0])+3600000
        if nxt<=cur: break
        cur=nxt
    # Need enough history for a meaningful annual test; newer listings are recorded as unavailable.
    if len(rows)<4000: raise RuntimeError(f'{sym}: insufficient OOS bars {len(rows)}')
    df=pd.DataFrame(rows,columns=['ot','open','high','low','close','volume','ct','quote_volume','trades','tb','tq','ignore'])
    for c in ['open','high','low','close']: df[c]=pd.to_numeric(df[c],errors='coerce')
    df['ts']=pd.to_datetime(df.ot,unit='ms',utc=True)
    return df.set_index('ts')[['open','high','low','close']].dropna()

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

def stats(events,h):
    vals=[]; mfe=[]; mae=[]
    for df,i in events:
        if i+h>=len(df): continue
        e=float(df.close.iloc[i]); f=float(df.close.iloc[i+h]); vals.append(-(f/e-1))
        # Short-direction excursions.
        mfe.append(max(0.0,1-float(df.low.iloc[i+1:i+h+1].min())/e))
        mae.append(max(0.0,float(df.high.iloc[i+1:i+h+1].max())/e-1))
    if not vals:return None
    a=np.asarray(vals,float); m=np.asarray(mfe,float); d=np.asarray(mae,float)
    med_mae=float(np.median(d)); ratio=float(np.median(m))/med_mae if med_mae>0 else float('inf')
    mean=float(a.mean()); med=float(np.median(a)); hit=float((a>0).mean())
    raw=bool(len(a)>=MIN_EVENTS and mean>=0.015 and med>0 and hit>0.55 and ratio>=2.0)
    return {'n':len(a),'mean':mean,'median':med,'hitRate':hit,'medianMFE':float(np.median(m)),'medianMAE':med_mae,'mfeMaeRatio':ratio,'pMeanPositive':pmean(a),'rawPass':raw}

def bh(rows):
    ps=sorted([(i,r['pMeanPositive']) for i,r in enumerate(rows)],key=lambda x:x[1]); m=len(ps); adj=[1.0]*m; run=1.0
    for rank in range(m,0,-1):
        i,p=ps[rank-1]; run=min(run,p*m/rank); adj[i]=min(1.0,run)
    for i,r in enumerate(rows): r['qValue']=adj[i]; r['pass']=bool(r['rawPass'] and adj[i]<=FDR_Q)

def main():
    t0=time.time(); data,fail=load_all(['BTCUSDT']+SYMBOLS)
    if 'BTCUSDT' not in data: raise RuntimeError('BTC OOS history unavailable')
    btc=data['BTCUSDT']; events=[]; per_symbol={}
    for s in SYMBOLS:
        if s not in data: continue
        # Align symbol and BTC without using future information.
        x=data[s].join(btc[['close']].rename(columns={'close':'btc'}),how='inner')
        if len(x)<4000: continue
        btc6=x.btc.pct_change(BTC_SHOCK_H); alt6=x.close.pct_change(BTC_SHOCK_H)
        sig=(btc6<=-BTC_SHOCK_ABS)&(alt6<=0)&(alt6>=btc6*UNDERREACTION_RATIO)
        ix=decluster(np.flatnonzero(sig.fillna(False).to_numpy()).tolist())
        per_symbol[s]=len(ix); events.extend((x[['open','high','low','close']],i) for i in ix)
    rows=[]
    for h in HORIZONS:
        st=stats(events,h)
        if st: rows.append({'family':'BTC_SHOCK_ALT_UNDERREACTION_V1:DOWN','horizon':h,**st})
    bh(rows)
    both=bool(len(rows)==2 and all(r['pass'] for r in rows))
    report={
      'engine':'BTC_SHOCK_UNDERREACTION_TEMPORAL_OOS_V1','authorization':'RESEARCH_ONLY','liveTrading':False,
      'candidate':'BTC_SHOCK_ALT_UNDERREACTION_V1:DOWN','period':{'start':START,'endExclusive':END},
      'discoveryPeriodApprox':{'start':'2025-09-11','end':'2026-09-11'},
      'frozenDefinition':{'btcReturnHours':BTC_SHOCK_H,'btcAbsShock':BTC_SHOCK_ABS,'underreactionRatio':UNDERREACTION_RATIO,'direction':'SHORT','declusterHours':MIN_GAP},
      'frozenHorizons':[48,72],
      'rawGate':{'minEvents':MIN_EVENTS,'mean':0.015,'median':'>0','hitRate':'>0.55','mfeMaeRatio':2.0},
      'multipleTesting':{'method':'Benjamini-Hochberg','q':FDR_Q},
      'universePolicy':'FROZEN_DISCOVERY_SYMBOL_SET_TEMPORAL_OOS_ONLY',
      'survivorshipBiasCleared':False,
      'symbolsFrozen':len(SYMBOLS),'symbolsLoaded':sorted([s for s in SYMBOLS if s in data]),'failures':fail,
      'eventCount':len(events),'eventsPerSymbol':per_symbol,'results':rows,
      'decision':'TEMPORAL_OOS_SURVIVOR_NEEDS_HISTORICAL_UNIVERSE_AUDIT' if both else 'REJECT_TEMPORAL_OOS',
      'liveReady':False,'runtimeSeconds':round(time.time()-t0,1)}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(report,indent=2,sort_keys=True)); print(json.dumps(report,indent=2,sort_keys=True))

if __name__=='__main__': main()
