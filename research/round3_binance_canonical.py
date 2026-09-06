#!/usr/bin/env python3
"""Run frozen Round 3 event families on public Binance Spot history.

Research only. No execution, sizing, or live authorization.
"""
from __future__ import annotations
import datetime as dt
import json
import pathlib
import time
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

from research.discovery_pipeline import evaluate_candidate

BASE_URL = "https://data-api.binance.vision"
INTERVAL = "1h"
DAYS = 365
MAX_SYMBOLS = 40
MIN_QV = 20_000_000
MAX_QV = 150_000_000
MAX_PRICE = 3.0
OUT = pathlib.Path("validation/edges/round3-canonical-latest.json")

MAJORS = {"BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","LTC","BCH","LINK","AVAX","DOT"}
EXCLUDED = {"USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","AEUR","TRY","BRL","GBP","AUD","USD1","RLUSD","USDE","PAXG","XAUT"}


def api(path: str):
    req = urllib.request.Request(BASE_URL + path, headers={"User-Agent":"tst-round3-canonical/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def universe():
    info = api("/api/v3/exchangeInfo")
    ticker = {x["symbol"]:x for x in api("/api/v3/ticker/24hr")}
    rows=[]
    for s in info.get("symbols",[]):
        base=s.get("baseAsset","")
        if s.get("status")!="TRADING" or s.get("quoteAsset")!="USDT" or not s.get("isSpotTradingAllowed"):
            continue
        if not base or base in MAJORS or base in EXCLUDED or base.endswith(("UP","DOWN","BULL","BEAR")):
            continue
        t=ticker.get(s["symbol"],{})
        px=float(t.get("lastPrice") or 0); qv=float(t.get("quoteVolume") or 0)
        if 0 < px <= MAX_PRICE and MIN_QV <= qv <= MAX_QV:
            rows.append((s["symbol"], qv))
    rows.sort(key=lambda x:x[1], reverse=True)
    return [s for s,_ in rows[:MAX_SYMBOLS]]


def klines(symbol: str):
    end=int(time.time()*1000); cur=end-DAYS*86400000; rows=[]
    while cur < end:
        q=urllib.parse.urlencode({"symbol":symbol,"interval":INTERVAL,"limit":1000,"startTime":cur,"endTime":end})
        batch=api("/api/v3/klines?"+q)
        if not batch: break
        rows.extend(batch)
        nxt=int(batch[-1][0])+3600000
        if nxt <= cur: break
        cur=nxt; time.sleep(0.01)
    if len(rows) < 4000:
        raise RuntimeError(f"{symbol}: insufficient bars {len(rows)}")
    df=pd.DataFrame(rows,columns=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"])
    for c in ["open","high","low","close","volume","quote_volume","taker_quote"]:
        df[c]=pd.to_numeric(df[c],errors="coerce")
    df["ts"]=pd.to_datetime(df["open_time"],unit="ms",utc=True)
    df=df.set_index("ts")[["open","high","low","close","volume","quote_volume","taker_quote"]].dropna()
    return df[~df.index.duplicated(keep="last")]


def zscore(s, n):
    mu=s.rolling(n).mean(); sd=s.rolling(n).std(ddof=0).replace(0,np.nan)
    return (s-mu)/sd


def rsi(c,n=14):
    d=c.diff(); up=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean(); dn=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    rs=up/dn.replace(0,np.nan)
    return (100-100/(1+rs)).fillna(50)


def candidate_masks(df, btc_close, breadth):
    c=df["close"]; qv=df["quote_volume"]; tq=df["taker_quote"]
    ret1=c.pct_change(); ret6=c.pct_change(6); ret24=c.pct_change(24)
    qv_rel=qv/qv.rolling(7*24).median().replace(0,np.nan)
    taker_share=tq/qv.replace(0,np.nan)
    r=rsi(c)

    # V1 frozen definitions. No future information appears in any mask.
    crash=(zscore(ret6,30*24)<=-2.5) & (qv_rel>=1.8) & (taker_share>=0.56) & (r<=30)

    btc=btc_close.reindex(df.index).ffill()
    residual=ret24-btc.pct_change(24)
    residual_z=zscore(residual,30*24)
    exhaustion=(ret1>0) & (taker_share>=0.54)
    dispersion=(residual_z<=-3.0) & exhaustion

    br=breadth.reindex(df.index).ffill()
    lag=btc.pct_change(6)-c.pct_change(6)
    continuation=(br>=0.65) & (btc.pct_change(6)>=0.012) & (lag>=0.008) & (c.pct_change(3)>-0.02) & (taker_share>=0.52)

    return {
        "LIQUIDITY_CRASH_EXHAUSTION_V1": crash.fillna(False),
        "EXTREME_RESIDUAL_DISPERSION_REVERSAL_V1": dispersion.fillna(False),
        "BREADTH_LEAD_LAG_CONTINUATION_V1": continuation.fillna(False),
    }


def breadth_series(data):
    frame=pd.concat([d["close"].pct_change(6).rename(s) for s,d in data.items() if s!="BTCUSDT"],axis=1)
    return (frame>0).mean(axis=1) if not frame.empty else pd.Series(dtype=float)


def serialize_evidence(ev):
    return {
        "name":ev.name,"events":ev.events,"passedHorizons":list(ev.passed_horizons),"pass":ev.all_required_pass,
        "gates":{str(h):{"n":g.n,"mean":g.mean_return,"median":g.median_return,"hitRate":g.hit_rate,"medianMFE":g.median_mfe,"medianMAE":g.median_mae,"mfeMaeRatio":g.mfe_mae_ratio,"pass":g.passed} for h,g in ev.gates.items()}
    }


symbols=universe(); data={}; failures={}
for s in ["BTCUSDT"]+symbols:
    try: data[s]=klines(s)
    except Exception as e: failures[s]=str(e)

btc=data.get("BTCUSDT")
if btc is None:
    raise RuntimeError("BTCUSDT history unavailable")
breadth=breadth_series(data)
pooled={k:[] for k in ["LIQUIDITY_CRASH_EXHAUSTION_V1","EXTREME_RESIDUAL_DISPERSION_REVERSAL_V1","BREADTH_LEAD_LAG_CONTINUATION_V1"]}
per_symbol=[]
for s in symbols:
    if s not in data: continue
    df=data[s]
    masks=candidate_masks(df,btc["close"],breadth)
    for name,mask in masks.items():
        idx=np.flatnonzero(mask.to_numpy()).tolist()
        ev=evaluate_candidate(name,df["close"].tolist(),df["high"].tolist(),df["low"].tolist(),idx,horizons=(24,48,72),direction=1,min_gap=12,min_events=20)
        per_symbol.append({"symbol":s,**serialize_evidence(ev)})
        for i in idx:
            if i+72 < len(df):
                pooled[name].append((s,i))

# Pooled raw evidence is aggregated as event-level forward returns across symbols.
def pooled_stats(name):
    vals={24:[],48:[],72:[]}; mfes={24:[],48:[],72:[]}; maes={24:[],48:[],72:[]}
    for s,i in pooled[name]:
        df=data[s]; entry=float(df["close"].iloc[i])
        for h in vals:
            vals[h].append(float(df["close"].iloc[i+h])/entry-1)
            mfes[h].append(float(df["high"].iloc[i+1:i+h+1].max())/entry-1)
            maes[h].append(max(0.0,1-float(df["low"].iloc[i+1:i+h+1].min())/entry))
    out={}
    for h in vals:
        a=np.asarray(vals[h],float); m=np.asarray(mfes[h],float); d=np.asarray(maes[h],float)
        if len(a)==0:
            out[str(h)]={"n":0,"pass":False}; continue
        ratio=float(np.median(m)/np.median(d)) if np.median(d)>0 else float("inf")
        mean=float(a.mean()); med=float(np.median(a)); hit=float((a>0).mean())
        passed=bool(len(a)>=30 and mean>=0.015 and med>0 and hit>0.55 and ratio>=2.0)
        out[str(h)]={"n":int(len(a)),"mean":mean,"median":med,"hitRate":hit,"medianMFE":float(np.median(m)),"medianMAE":float(np.median(d)),"mfeMaeRatio":ratio,"pass":passed}
    return {"name":name,"events":len(pooled[name]),"gates":out,"pass":any(x.get("pass") for x in out.values())}

report={
    "engine":"ROUND3_CANONICAL_BINANCE","authorization":"RESEARCH_ONLY","liveTrading":False,"interval":INTERVAL,"days":DAYS,
    "frozenDefinitions":True,"symbolsRequested":symbols,"symbolsTested":[s for s in symbols if s in data],"families":[pooled_stats(k) for k in pooled],
    "perSymbol":per_symbol,"dataFailures":failures,"generatedAt":dt.datetime.now(dt.timezone.utc).isoformat()
}
OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(report,indent=2,allow_nan=False)); print(json.dumps(report,indent=2,allow_nan=False))
