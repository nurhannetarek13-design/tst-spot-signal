#!/usr/bin/env python3
"""
Research-only public edge lab for Binance Spot.
Tests pre-registered, literature-backed edge families on the same liquid small/mid-cap
universe used by the minute scanner. No live authorization is possible from this file.
"""
import datetime as dt
import json
import math
import pathlib
import time
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

BASE_URL = "https://data-api.binance.vision"
INTERVAL = "1h"
DAYS = 120
MAX_SYMBOLS = 60
MIN_QV = 20_000_000
MAX_QV = 150_000_000
MAX_PRICE = 3.0
STAKE = 5.5
BASE_COST_PER_SIDE = 0.0015   # 0.10% fee + 0.05% slippage assumption
STRESS_COST_PER_SIDE = 0.0030 # doubled friction
OUT = pathlib.Path("validation/edges/public-edge-lab-latest.json")
STRATEGY_ID = "TST_PUBLIC_EDGE_LAB_V1"

MAJORS = {"BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","LTC","BCH","LINK","AVAX","DOT"}
EXCLUDED = {"USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","AEUR","TRY","BRL","GBP","AUD","USD1","RLUSD","USDE","PAXG","XAUT"}

def api(path):
    req=urllib.request.Request(BASE_URL+path,headers={"User-Agent":"tst-public-edge-lab/1.0"})
    with urllib.request.urlopen(req,timeout=30) as r:
        return json.load(r)

def allowed_base(base):
    if not base or base in EXCLUDED or base in MAJORS:
        return False
    if base.endswith(("UP","DOWN","BULL","BEAR")):
        return False
    return True

def universe():
    info=api("/api/v3/exchangeInfo")
    tick={x["symbol"]:x for x in api("/api/v3/ticker/24hr")}
    rows=[]
    for s in info.get("symbols",[]):
        if s.get("status")!="TRADING" or s.get("quoteAsset")!="USDT" or not s.get("isSpotTradingAllowed"):
            continue
        base=s.get("baseAsset","")
        if not allowed_base(base):
            continue
        t=tick.get(s["symbol"],{})
        px=float(t.get("lastPrice") or 0)
        qv=float(t.get("quoteVolume") or 0)
        if not (0 < px <= MAX_PRICE and MIN_QV <= qv <= MAX_QV):
            continue
        rows.append({"symbol":s["symbol"],"base":base,"price":px,"quoteVolume24h":qv})
    rows.sort(key=lambda x:x["quoteVolume24h"],reverse=True)
    return rows[:MAX_SYMBOLS]

def klines(symbol):
    end=int(time.time()*1000)
    start=end-DAYS*86400000
    rows=[]
    cur=start
    while cur<end:
        q=urllib.parse.urlencode({"symbol":symbol,"interval":INTERVAL,"limit":1000,"startTime":cur,"endTime":end})
        batch=api("/api/v3/klines?"+q)
        if not batch:
            break
        rows.extend(batch)
        nxt=int(batch[-1][0])+3600000
        if nxt<=cur:
            break
        cur=nxt
        time.sleep(0.01)
    if len(rows)<1200:
        raise RuntimeError(f"{symbol}: insufficient bars {len(rows)}")
    df=pd.DataFrame(rows,columns=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"])
    for c in ["open","high","low","close","volume","quote_volume","taker_quote"]:
        df[c]=pd.to_numeric(df[c],errors="coerce")
    df["ts"]=pd.to_datetime(df["open_time"],unit="ms",utc=True)
    return df.set_index("ts")[["open","high","low","close","volume","quote_volume","taker_quote"]].dropna()

def ema(s,n): return s.ewm(span=n,adjust=False).mean()

def rsi(s,n=14):
    d=s.diff()
    g=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean()
    l=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    rs=g/l.replace(0,np.nan)
    return (100-100/(1+rs)).fillna(50)

def atr_pct(df,n=14):
    pc=df["close"].shift(1)
    tr=pd.concat([(df["high"]-df["low"]).abs(),(df["high"]-pc).abs(),(df["low"]-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()/df["close"]

def leader_series(data):
    cols=[]
    for s in ["BTCUSDT","ETHUSDT","SOLUSDT"]:
        if s in data:
            cols.append(data[s]["close"].pct_change(3).rename(s))
    return pd.concat(cols,axis=1).mean(axis=1) if cols else pd.Series(dtype=float)

def signals(df,family,leader=None):
    c=df["close"]; qv=df["quote_volume"]
    rv=qv/qv.rolling(24).median().replace(0,np.nan)
    r=rsi(c)
    a=atr_pct(df)
    e24,e48,e120=ema(c,24),ema(c,48),ema(c,120)

    if family=="TS_MOMENTUM":
        sig=(c>e48)&(e48>e120)&(c.pct_change(24)>0.02)&a.between(0.006,0.08)&(rv>=0.8)
        return sig.fillna(False), {"hold":24,"sl":0.03,"tp":0.06}

    if family=="CROSS_CRYPTO_LEAD_LAG":
        if leader is None or leader.empty:
            return pd.Series(False,index=df.index), {"hold":6,"sl":0.025,"tp":0.05}
        lead=leader.reindex(df.index).ffill()
        alt3=c.pct_change(3)
        gap=lead-alt3
        sig=(lead>=0.012)&(gap>=0.008)&(alt3>-0.02)&(c>e24)&(rv>=0.9)&r.between(42,70)
        return sig.fillna(False), {"hold":6,"sl":0.025,"tp":0.05}

    if family=="LIQUIDITY_REVERSAL":
        ret6=c.pct_change(6)
        mu=ret6.rolling(30*24).mean()
        sd=ret6.rolling(30*24).std(ddof=0).replace(0,np.nan)
        z=(ret6-mu)/sd
        detrended=qv/qv.rolling(7*24).median().replace(0,np.nan)
        sig=(z<=-2.0)&(detrended<=1.10)&(r<=35)&(c<e24)
        return sig.fillna(False), {"hold":12,"sl":0.03,"tp":0.05}

    if family=="VOLATILITY_BREAKOUT":
        comp=a.rolling(72).rank(pct=True)<=0.25
        hh=df["high"].rolling(24).max().shift(1)
        sig=comp.shift(1).fillna(False)&(c>hh)&(rv>=1.5)&r.between(55,75)
        return sig.fillna(False), {"hold":12,"sl":0.025,"tp":0.055}

    raise ValueError(family)

def backtest(df,sig,rules,cost):
    trades=[]
    i=0
    n=len(df)
    while i<n-2:
        if not bool(sig.iloc[i]):
            i+=1; continue
        entry_i=i+1
        entry=float(df["open"].iloc[entry_i])
        if not entry>0:
            i+=1; continue
        stop=entry*(1-rules["sl"]); target=entry*(1+rules["tp"])
        exit_i=min(n-1,entry_i+rules["hold"])
        exit_px=float(df["close"].iloc[exit_i]); reason="TIME"
        for j in range(entry_i,exit_i+1):
            lo=float(df["low"].iloc[j]); hi=float(df["high"].iloc[j])
            # pessimistic ordering if both touched
            if lo<=stop:
                exit_i=j; exit_px=stop; reason="STOP"; break
            if hi>=target:
                exit_i=j; exit_px=target; reason="TARGET"; break
        gross=STAKE*(exit_px/entry-1)
        fees=STAKE*cost + (STAKE*(exit_px/entry))*cost
        pnl=gross-fees
        trades.append({"exit":df.index[exit_i].isoformat(),"pnl":pnl,"reason":reason})
        i=exit_i+1
    return trades

def metrics(trades):
    p=np.array([x["pnl"] for x in trades],dtype=float)
    if len(p)==0:
        return {"trades":0,"wins":0,"winRate":0.0,"netPnlUSDT":0.0,"expectancyUSDT":0.0,"profitFactor":0.0,"maxDrawdownUSDT":0.0}
    gp=float(p[p>0].sum()) if np.any(p>0) else 0.0
    gl=float(-p[p<0].sum()) if np.any(p<0) else 0.0
    eq=np.cumsum(p); peak=np.maximum.accumulate(np.r_[0.0,eq])[:-1]
    return {
        "trades":int(len(p)),"wins":int((p>0).sum()),"winRate":float((p>0).mean()),
        "netPnlUSDT":float(p.sum()),"expectancyUSDT":float(p.mean()),
        "profitFactor":float(gp/gl) if gl>0 else (999.0 if gp>0 else 0.0),
        "maxDrawdownUSDT":float(np.maximum(0,peak-eq).max(initial=0.0))
    }

def family_gate(base,stress):
    return bool(
        base["trades"]>=40 and base["expectancyUSDT"]>0 and base["profitFactor"]>=1.15
        and stress["expectancyUSDT"]>0 and stress["profitFactor"]>=1.0
        and base["maxDrawdownUSDT"]<=2.0
    )

rows=universe()
data={}
failures={}
for anchor in ["BTCUSDT","ETHUSDT","SOLUSDT"]:
    try: data[anchor]=klines(anchor)
    except Exception as e: failures[anchor]=str(e)
for x in rows:
    try: data[x["symbol"]]=klines(x["symbol"])
    except Exception as e: failures[x["symbol"]]=str(e)

leader=leader_series(data)
families=["TS_MOMENTUM","CROSS_CRYPTO_LEAD_LAG","LIQUIDITY_REVERSAL","VOLATILITY_BREAKOUT"]
family_results=[]
symbol_results=[]

for fam in families:
    pooled_base=[]; pooled_stress=[]
    for x in rows:
        s=x["symbol"]
        if s not in data: continue
        df=data[s]
        # warmup first 60%; final 40% is untouched evaluation for these fixed rules
        start=int(len(df)*0.60)
        sub=df.iloc[start:].copy()
        lead_sub=leader.reindex(sub.index).ffill() if not leader.empty else None
        sig,rules=signals(sub,fam,lead_sub)
        tb=backtest(sub,sig,rules,BASE_COST_PER_SIDE)
        ts=backtest(sub,sig,rules,STRESS_COST_PER_SIDE)
        mb,ms=metrics(tb),metrics(ts)
        pooled_base.extend(tb); pooled_stress.extend(ts)
        if mb["trades"]>=5:
            symbol_results.append({
                "family":fam,"symbol":s,"base":mb,"stress2x":ms,
                "paperCandidate":bool(mb["trades"]>=10 and mb["expectancyUSDT"]>0 and ms["expectancyUSDT"]>0 and ms["profitFactor"]>=0.95)
            })
    fb,fs=metrics(pooled_base),metrics(pooled_stress)
    family_results.append({"family":fam,"base":fb,"stress2x":fs,"edgePass":family_gate(fb,fs)})

family_results.sort(key=lambda x:(x["edgePass"],x["stress2x"]["expectancyUSDT"],x["base"]["profitFactor"]),reverse=True)
symbol_results.sort(key=lambda x:(x["paperCandidate"],x["stress2x"]["expectancyUSDT"],x["base"]["profitFactor"]),reverse=True)
selected=next((x for x in family_results if x["edgePass"]),None)

report={
    "engine":"PUBLIC_EDGE_LAB","strategyId":STRATEGY_ID,
    "status":"EDGE_FOUND" if selected else "NO_EDGE_PASS",
    "pass":bool(selected),"authorization":"RESEARCH_ONLY","liveTrading":False,
    "universeMode":"LIQUID_SMALL_MID_CAP_USDT","universe":rows,
    "symbolsTested":[x for x in data.keys() if x not in {"BTCUSDT","ETHUSDT","SOLUSDT"}],
    "families":family_results,"topSymbolCandidates":symbol_results[:20],
    "costs":{"basePerSide":BASE_COST_PER_SIDE,"stressPerSide":STRESS_COST_PER_SIDE},
    "split":{"warmup":0.60,"evaluation":0.40},
    "notes":[
        "Rules are preregistered in code and are not auto-tuned.",
        "Signals enter on next-bar open to avoid same-bar lookahead.",
        "If stop and target touch on the same bar, stop is assumed first.",
        "A pass can authorize further forward-paper validation only, never live trading."
    ],
    "dataFailures":failures,"generatedAt":dt.datetime.now(dt.timezone.utc).isoformat()
}
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
