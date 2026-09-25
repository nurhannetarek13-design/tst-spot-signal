#!/usr/bin/env python3
"""Two-era evaluation for Indicator Fusion V2.

The recent 365d window is development data because V1 already exposed it.
The preceding historical window is the fresh holdout for V2.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from research.indicator_fusion_v2 import DEFAULT_PARAMS_V2, compute_indicator_fusion_v2, contract_v2, risk_geometry_v2

BASE_URL="https://data-api.binance.vision"
INTERVAL="1h"
DAYS=730
MAX_SYMBOLS=40
MIN_QV=20_000_000.0
MAX_QV=500_000_000.0
STAKE=5.5
BASE_COST=.0015
STRESS_COST=.003
OUT=pathlib.Path("validation/edges/indicator-fusion-v2-latest.json")
MAJORS={"BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","LTC","BCH","LINK","AVAX","DOT"}
EXCLUDED={"USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","AEUR","TRY","BRL","GBP","AUD","USD1","RLUSD","USDE","PAXG","XAUT"}

def api(path):
    req=urllib.request.Request(BASE_URL+path,headers={"User-Agent":"tst-indicator-fusion-v2/1.0"})
    with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)

def universe():
    info=api("/api/v3/exchangeInfo"); tick={x["symbol"]:x for x in api("/api/v3/ticker/24hr")}
    rows=[]
    for s in info.get("symbols",[]):
        if s.get("status")!="TRADING" or s.get("quoteAsset")!="USDT" or not s.get("isSpotTradingAllowed"):continue
        b=s.get("baseAsset","")
        if not b or b in EXCLUDED or b in MAJORS or b.endswith(("UP","DOWN","BULL","BEAR")):continue
        t=tick.get(s["symbol"],{}); qv=float(t.get("quoteVolume") or 0); px=float(t.get("lastPrice") or 0)
        if px>0 and MIN_QV<=qv<=MAX_QV:rows.append({"symbol":s["symbol"],"base":b,"price":px,"quoteVolume24h":qv})
    rows.sort(key=lambda x:x["quoteVolume24h"],reverse=True)
    return rows[:MAX_SYMBOLS]

def klines(symbol):
    end=int(time.time()*1000); start=end-DAYS*86400000; rows=[]; cur=start
    while cur<end:
        q=urllib.parse.urlencode({"symbol":symbol,"interval":INTERVAL,"limit":1000,"startTime":cur,"endTime":end})
        batch=api("/api/v3/klines?"+q)
        if not batch:break
        rows.extend(batch); nxt=int(batch[-1][0])+3600000
        if nxt<=cur:break
        cur=nxt; time.sleep(.01)
    if len(rows)<4000:raise RuntimeError(f"{symbol}: insufficient bars {len(rows)}")
    d=pd.DataFrame(rows,columns=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"])
    for c in ["open","high","low","close","volume","quote_volume","taker_quote"]:d[c]=pd.to_numeric(d[c],errors="coerce")
    d["ts"]=pd.to_datetime(d["open_time"],unit="ms",utc=True)
    return d.set_index("ts")[["open","high","low","close","volume","quote_volume","taker_quote"]].dropna()

def backtest(symbol,df,f,start,end,cost):
    trades=[]; i=max(0,start); end=min(end,len(df))
    while i<end-1:
        if not bool(f["enter"].iloc[i]):i+=1;continue
        e=i+1
        if e>=end:break
        entry=float(df["open"].iloc[e]); atr=float(f["atr_pct"].iloc[i])
        if entry<=0 or not np.isfinite(atr) or atr<=0:i+=1;continue
        risk,target,hold=risk_geometry_v2(atr)
        stop=entry*(1-risk); take=entry*(1+target); x=min(end-1,e+hold); px=float(df["close"].iloc[x]); reason="TIME"
        for j in range(e,x+1):
            lo=float(df["low"].iloc[j]); hi=float(df["high"].iloc[j])
            if lo<=stop:x=j;px=stop;reason="STOP";break
            if hi>=take:x=j;px=take;reason="TARGET";break
        gross=STAKE*(px/entry-1); fees=STAKE*cost+(STAKE*(px/entry))*cost
        trades.append({"symbol":symbol,"exit":df.index[x].isoformat(),"pnl":float(gross-fees),"reason":reason,"score":float(f["score"].iloc[i]),"vr":float(f["variance_ratio"].iloc[i])})
        i=x+1
    return trades

def metrics(trades):
    p=np.asarray([x["pnl"] for x in sorted(trades,key=lambda z:(z["exit"],z["symbol"]))],dtype=float)
    if not len(p):return {"trades":0,"wins":0,"winRate":0.0,"netPnlUSDT":0.0,"expectancyUSDT":0.0,"profitFactor":0.0,"maxDrawdownUSDT":0.0}
    gp=float(p[p>0].sum()) if np.any(p>0) else 0.; gl=float(-p[p<0].sum()) if np.any(p<0) else 0.; eq=np.cumsum(p); peak=np.maximum.accumulate(np.r_[0.,eq])[:-1]
    return {"trades":int(len(p)),"wins":int((p>0).sum()),"winRate":float((p>0).mean()),"netPnlUSDT":float(p.sum()),"expectancyUSDT":float(p.mean()),"profitFactor":float(gp/gl) if gl>0 else (999. if gp>0 else 0.),"maxDrawdownUSDT":float(np.maximum(0,peak-eq).max(initial=0.0))}

def gate(m,min_trades=40,min_pf=1.05,max_dd=5.0):
    return m["trades"]>=min_trades and m["expectancyUSDT"]>0 and m["profitFactor"]>=min_pf and m["maxDrawdownUSDT"]<=max_dd

def main():
    rows=universe(); data={}; failures={}
    with ThreadPoolExecutor(max_workers=8) as ex:
        fs={ex.submit(klines,x["symbol"]):x["symbol"] for x in rows}
        for fut in as_completed(fs):
            s=fs[fut]
            try:data[s]=fut.result()
            except Exception as e:failures[s]=str(e)
    symbols=[x["symbol"] for x in rows if x["symbol"] in data]
    cutoff=pd.Timestamp.now(tz="UTC")-pd.Timedelta(days=365)
    pooled={"olderHoldout":{"base":[],"stress2x":[]},"recentDevelopment":{"base":[],"stress2x":[]}}
    per={}; older_symbols=[]; recent_symbols=[]
    for s in symbols:
        d=data[s]; f=compute_indicator_fusion_v2(d,DEFAULT_PARAMS_V2)
        cut=int(np.searchsorted(d.index.view("int64"),cutoff.value))
        per[s]={}
        if cut>=4000:
            older_symbols.append(s)
            b=backtest(s,d,f,0,cut,BASE_COST); z=backtest(s,d,f,0,cut,STRESS_COST)
            pooled["olderHoldout"]["base"]+=b; pooled["olderHoldout"]["stress2x"]+=z
            per[s]["olderHoldout"]={"base":metrics(b),"stress2x":metrics(z)}
        if len(d)-cut>=4000:
            recent_symbols.append(s)
            b=backtest(s,d,f,cut,len(d),BASE_COST); z=backtest(s,d,f,cut,len(d),STRESS_COST)
            pooled["recentDevelopment"]["base"]+=b; pooled["recentDevelopment"]["stress2x"]+=z
            per[s]["recentDevelopment"]={"base":metrics(b),"stress2x":metrics(z)}
    agg={k:{"base":metrics(v["base"]),"stress2x":metrics(v["stress2x"])} for k,v in pooled.items()}
    def breadth(section,slist):
        good=[s for s in slist if per.get(s,{}).get(section,{}).get("stress2x",{}).get("netPnlUSDT",0)>0 and per[s][section]["stress2x"]["trades"]>0]
        counts={s:per[s][section]["base"]["trades"] for s in slist if section in per.get(s,{})}
        total=max(1,sum(counts.values())); share=(max(counts.values())/total) if counts else 1.0
        return {"profitableStressSymbols":good,"profitableCount":len(good),"totalSymbols":len(slist),"largestTradeShare":float(share)}
    br_old=breadth("olderHoldout",older_symbols); br_recent=breadth("recentDevelopment",recent_symbols)
    passed=(
        len(older_symbols)>=10 and len(recent_symbols)>=12
        and gate(agg["olderHoldout"]["base"],50,1.10,5.0)
        and gate(agg["olderHoldout"]["stress2x"],50,1.00,6.0)
        and gate(agg["recentDevelopment"]["base"],50,1.10,5.0)
        and gate(agg["recentDevelopment"]["stress2x"],50,1.00,6.0)
        and br_old["profitableCount"]>=max(4,len(older_symbols)//4)
        and br_recent["profitableCount"]>=max(4,len(recent_symbols)//4)
        and br_old["largestTradeShare"]<=0.30 and br_recent["largestTradeShare"]<=0.30
    )
    report={
      "engine":"INDICATOR_FUSION_V2_EVENT_REGIME","status":"TWO_ERA_EDGE_FOUND" if passed else "NO_TWO_ERA_EDGE_PASS","pass":bool(passed),
      "authorization":"RESEARCH_ONLY","liveTrading":False,"automaticPromotion":False,"parameterSearch":False,
      "developmentContamination":{"recent365dSeenInV1":True,"olderHoldoutSeenInV1":False},
      "timeframe":INTERVAL,"daysRequested":DAYS,"universe":rows,"symbolsLoaded":symbols,"dataFailures":failures,
      "olderHoldoutSymbols":older_symbols,"recentDevelopmentSymbols":recent_symbols,"aggregate":agg,
      "breadth":{"olderHoldout":br_old,"recentDevelopment":br_recent},"perSymbol":per,
      "config":contract_v2(),
      "candidateSpec":{"family":"INDICATOR_FUSION_V2_EVENT_REGIME","timeframe":"1h","symbols":older_symbols[:5],"params":dict(DEFAULT_PARAMS_V2),"eligibleForIndependentValidation":bool(passed),"liveTrading":False},
      "notes":["Recent 365d is development-only because V1 exposed it.","The preceding available history is V2's fresh holdout.","No parameter search or per-symbol outcome selection.","First-breakout event + variance-ratio regime reduces repeated correlated entries.","Pass can only advance to independent validators/forward paper."],
      "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat()
    }
    OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(report,indent=2))
    print(json.dumps({"status":report["status"],"aggregate":agg,"breadth":report["breadth"],"olderSymbols":len(older_symbols),"recentSymbols":len(recent_symbols)},indent=2))

if __name__=="__main__":main()
