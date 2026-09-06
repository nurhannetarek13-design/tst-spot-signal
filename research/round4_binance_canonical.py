#!/usr/bin/env python3
"""Round 4 orthogonal candidate discovery on public Binance Spot history.

Research only. New hypotheses only; no V1 threshold retuning.
"""
from __future__ import annotations
import datetime as dt, json, pathlib, time, urllib.parse, urllib.request
import numpy as np
import pandas as pd
from research.discovery_pipeline import evaluate_candidate

BASE_URL='https://data-api.binance.vision'; INTERVAL='1h'; DAYS=365
MAX_SYMBOLS=40; MIN_QV=20_000_000; MAX_QV=150_000_000; MAX_PRICE=3.0
OUT=pathlib.Path('validation/edges/round4-canonical-latest.json')
MAJORS={'BTC','ETH','BNB','SOL','XRP','ADA','DOGE','TRX','LTC','BCH','LINK','AVAX','DOT'}
EXCLUDED={'USDC','FDUSD','TUSD','USDP','DAI','BUSD','EUR','AEUR','TRY','BRL','GBP','AUD','USD1','RLUSD','USDE','PAXG','XAUT'}
FAMILIES=('VOL_CONTRACTION_BREAKOUT_V1','JUMP_ABSORPTION_REVERSAL_V1','ORDERFLOW_TREND_CONTINUATION_V1')

def api(path):
    req=urllib.request.Request(BASE_URL+path,headers={'User-Agent':'tst-round4-canonical/1.0'})
    with urllib.request.urlopen(req,timeout=30) as r: return json.load(r)

def universe():
    info=api('/api/v3/exchangeInfo'); ticker={x['symbol']:x for x in api('/api/v3/ticker/24hr')}; rows=[]
    for s in info.get('symbols',[]):
        base=s.get('baseAsset','')
        if s.get('status')!='TRADING' or s.get('quoteAsset')!='USDT' or not s.get('isSpotTradingAllowed'): continue
        if not base or base in MAJORS or base in EXCLUDED or base.endswith(('UP','DOWN','BULL','BEAR')): continue
        t=ticker.get(s['symbol'],{}); px=float(t.get('lastPrice') or 0); qv=float(t.get('quoteVolume') or 0)
        if 0<px<=MAX_PRICE and MIN_QV<=qv<=MAX_QV: rows.append((s['symbol'],qv))
    rows.sort(key=lambda x:x[1],reverse=True); return [s for s,_ in rows[:MAX_SYMBOLS]]

def klines(symbol):
    end=int(time.time()*1000); cur=end-DAYS*86400000; rows=[]
    while cur<end:
        q=urllib.parse.urlencode({'symbol':symbol,'interval':INTERVAL,'limit':1000,'startTime':cur,'endTime':end})
        b=api('/api/v3/klines?'+q)
        if not b: break
        rows.extend(b); nxt=int(b[-1][0])+3600000
        if nxt<=cur: break
        cur=nxt; time.sleep(0.01)
    if len(rows)<4000: raise RuntimeError(f'{symbol}: insufficient bars {len(rows)}')
    df=pd.DataFrame(rows,columns=['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_base','taker_quote','ignore'])
    for c in ['open','high','low','close','volume','quote_volume','taker_quote']: df[c]=pd.to_numeric(df[c],errors='coerce')
    df['ts']=pd.to_datetime(df['open_time'],unit='ms',utc=True)
    return df.set_index('ts')[['open','high','low','close','volume','quote_volume','taker_quote']].dropna()

def zscore(s,n):
    mu=s.rolling(n).mean(); sd=s.rolling(n).std(ddof=0).replace(0,np.nan); return (s-mu)/sd

def masks(df,btc):
    c=df.close; h=df.high; l=df.low; qv=df.quote_volume; tq=df.taker_quote
    r1=c.pct_change(); r6=c.pct_change(6); r24=c.pct_change(24)
    atr=(h-l).rolling(24).mean()/c
    rv24=r1.rolling(24).std(ddof=0); rv7d=r1.rolling(7*24).std(ddof=0)
    taker=tq/qv.replace(0,np.nan); qvrel=qv/qv.rolling(7*24).median().replace(0,np.nan)
    btc=btc.reindex(df.index).ffill(); btc6=btc.pct_change(6)

    # New hypothesis 1: compression followed by high-volume breakout in a supportive BTC tape.
    hh=c.rolling(48).max().shift(1)
    vol_break=(rv24 <= rv7d*0.65) & (c>hh) & (qvrel>=1.5) & (taker>=0.56) & (btc6>-0.01)

    # New hypothesis 2: statistically large negative return followed immediately by buy-side absorption.
    jump_down=zscore(r1,30*24)<=-3.0
    absorption=(r1>0) & (taker>=0.58) & (qvrel>=1.5)
    jump_rev=jump_down.shift(1,fill_value=False) & absorption

    # New hypothesis 3: persistent relative momentum with strong aggressor confirmation.
    ema24=c.ewm(span=24,adjust=False).mean(); ema72=c.ewm(span=72,adjust=False).mean()
    rel6=r6-btc6
    orderflow=(ema24>ema72) & (r24>0.03) & (rel6>0.005) & (taker>=0.60) & (qvrel>=1.2) & (atr<0.08)
    return {'VOL_CONTRACTION_BREAKOUT_V1':vol_break.fillna(False),'JUMP_ABSORPTION_REVERSAL_V1':jump_rev.fillna(False),'ORDERFLOW_TREND_CONTINUATION_V1':orderflow.fillna(False)}

def safe_float(x):
    x=float(x)
    return x if np.isfinite(x) else None

def ser(ev):
    return {'name':ev.name,'events':ev.events,'passedHorizons':list(ev.passed_horizons),'pass':ev.all_required_pass,
            'gates':{str(h):{'n':g.n,'mean':safe_float(g.mean_return),'median':safe_float(g.median_return),'hitRate':safe_float(g.hit_rate),'medianMFE':safe_float(g.median_mfe),'medianMAE':safe_float(g.median_mae),'mfeMaeRatio':safe_float(g.mfe_mae_ratio),'pass':g.passed} for h,g in ev.gates.items()}}

symbols=universe(); data={}; failures={}
for s in ['BTCUSDT']+symbols:
    try: data[s]=klines(s)
    except Exception as e: failures[s]=str(e)
if 'BTCUSDT' not in data: raise RuntimeError('BTCUSDT history unavailable')
pooled={k:[] for k in FAMILIES}; per=[]
for s in symbols:
    if s not in data: continue
    df=data[s]
    for name,mask in masks(df,data['BTCUSDT'].close).items():
        idx=np.flatnonzero(mask.to_numpy()).tolist()
        ev=evaluate_candidate(name,df.close.tolist(),df.high.tolist(),df.low.tolist(),idx,horizons=(24,48,72),direction=1,min_gap=12,min_events=20)
        per.append({'symbol':s,**ser(ev)})
        for i in idx:
            if i+72<len(df): pooled[name].append((s,i))

def pooled_stats(name):
    vals={24:[],48:[],72:[]}; mf={24:[],48:[],72:[]}; ma={24:[],48:[],72:[]}
    for s,i in pooled[name]:
        df=data[s]; e=float(df.close.iloc[i])
        for h in vals:
            vals[h].append(float(df.close.iloc[i+h])/e-1)
            mf[h].append(float(df.high.iloc[i+1:i+h+1].max())/e-1)
            ma[h].append(max(0.0,1-float(df.low.iloc[i+1:i+h+1].min())/e))
    out={}
    for h in vals:
        a=np.asarray(vals[h],float); m=np.asarray(mf[h],float); d=np.asarray(ma[h],float)
        if len(a)==0: out[str(h)]={'n':0,'pass':False}; continue
        med_d=float(np.median(d)); ratio=(float(np.median(m))/med_d) if med_d>0 else None
        mean=float(a.mean()); med=float(np.median(a)); hit=float((a>0).mean())
        passed=bool(len(a)>=30 and mean>=0.015 and med>0 and hit>0.55 and ((ratio is None) or ratio>=2.0))
        out[str(h)]={'n':int(len(a)),'mean':mean,'median':med,'hitRate':hit,'medianMFE':float(np.median(m)),'medianMAE':med_d,'mfeMaeRatio':ratio,'pass':passed}
    return {'name':name,'events':len(pooled[name]),'gates':out,'pass':any(v.get('pass',False) for v in out.values())}

report={'engine':'ROUND4_CANONICAL_BINANCE','authorization':'RESEARCH_ONLY','liveTrading':False,'interval':INTERVAL,'days':DAYS,'frozenDefinitions':True,'symbolsRequested':symbols,'symbolsTested':[s for s in symbols if s in data],'families':[pooled_stats(k) for k in FAMILIES],'perSymbol':per,'dataFailures':failures,'generatedAt':dt.datetime.now(dt.timezone.utc).isoformat()}
OUT.parent.mkdir(parents=True,exist_ok=True); txt=json.dumps(report,indent=2,allow_nan=False); OUT.write_text(txt); print(txt)
