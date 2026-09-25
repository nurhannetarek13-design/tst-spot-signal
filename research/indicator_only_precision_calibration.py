#!/usr/bin/env python3
"""Precision-first calibration for INDICATOR_ONLY_V1.

Goal: test whether a very selective indicator/risk configuration can honestly
support a ~99% profitable-trade claim. It must also retain positive net PnL
after configured fees/slippage and enough trades to be statistically meaningful.

No runtime config is changed by this script. The final holdout is never used
for parameter selection.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import math
import pathlib
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

CFG=json.loads(pathlib.Path("ready_bot/indicator_config.json").read_text())
BASE="https://data-api.binance.vision"
DAYS=540
DEV_EXCLUDE_DAYS=180
CAL_WINDOW_DAYS=360
TRAIN_DAYS=180
VALIDATION_DAYS=90
HOLDOUT_DAYS=90
OUT=pathlib.Path("validation/indicator-only-precision-calibration-latest.json")
FEE=float(CFG["risk"]["fee_rate"])
SLIP=float(CFG["risk"]["slippage_rate"])
MAX_FORWARD_BARS=7*24*4

SCORE_MIN=[90,95,100]
TAKER_MIN=[0.60,0.64,0.68,0.72]
TAKER3_MIN=[0.57,0.60,0.63]
RVOL_MIN=[1.5,2.0,2.5,3.0]
RSI_MAX=[62,66,70]
STOP_MULT=[1.5,2.0,2.5]
REWARD_RISK=[0.35,0.5,0.75,1.0]

MIN_TRAIN_TRADES=100
MIN_VALIDATION_TRADES=50
MIN_HOLDOUT_CLAIM_TRADES=100

# This legacy calibration downloads only today's listed symbols from public REST.
# It is useful diagnostically, but cannot support a production precision claim.
POINT_IN_TIME_UNIVERSE=False
DELISTED_COVERAGE=False


def api(path):
    req=urllib.request.Request(BASE+path,headers={"User-Agent":"tst-precision-calibration/1.0"})
    with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)


def universe():
    info=api("/api/v3/exchangeInfo")
    tick={x["symbol"]:x for x in api("/api/v3/ticker/24hr")}
    stable={"USDC","FDUSD","TUSD","USDP","DAI","BUSD","USD1","RLUSD","USDE","EUR","AEUR","TRY","BRL","GBP","AUD"}
    rows=[]
    for s in info.get("symbols",[]):
        if s.get("status")!="TRADING" or s.get("quoteAsset")!="USDT" or not s.get("isSpotTradingAllowed",True):continue
        b=s.get("baseAsset","")
        if not b or b in stable or b.endswith(("UP","DOWN","BULL","BEAR")):continue
        t=tick.get(s["symbol"],{})
        qv=float(t.get("quoteVolume") or 0); px=float(t.get("lastPrice") or 0)
        if px>0 and qv>=float(CFG["universe"]["min_quote_volume_24h"]):
            rows.append({"symbol":s["symbol"],"quoteVolume24h":qv})
    rows.sort(key=lambda x:x["quoteVolume24h"],reverse=True)
    return rows[:int(CFG["universe"]["max_symbols"])]


def fetch_15m(symbol):
    end=int(time.time()*1000); start=end-DAYS*86400000
    rows=[]; cur=start
    while cur<end:
        q=urllib.parse.urlencode({"symbol":symbol,"interval":"15m","limit":1000,"startTime":cur,"endTime":end})
        batch=api("/api/v3/klines?"+q)
        if not batch:break
        rows.extend(batch)
        nxt=int(batch[-1][0])+900000
        if nxt<=cur:break
        cur=nxt
        time.sleep(.004)
    if len(rows)<12000:raise RuntimeError(f"{symbol}: insufficient bars {len(rows)}")
    d=pd.DataFrame(rows,columns=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"])
    for c in ["open","high","low","close","volume","quote_volume","taker_quote"]:d[c]=pd.to_numeric(d[c],errors="coerce")
    d["ts_close"]=pd.to_datetime(d["close_time"],unit="ms",utc=True)
    return d.set_index("ts_close")[["open","high","low","close","volume","quote_volume","taker_quote"]].dropna()


def resample(df,rule):
    return df.resample(rule,label="right",closed="right").agg({
        "open":"first","high":"max","low":"min","close":"last",
        "volume":"sum","quote_volume":"sum","taker_quote":"sum",
    }).dropna()


def ema_seeded(s,n):
    x=s.to_numpy(float); out=np.full(len(x),np.nan)
    if len(x)<n:return pd.Series(out,index=s.index)
    a=2/(n+1); e=float(np.mean(x[:n]));out[n-1]=e
    for i in range(n,len(x)):
        e=a*x[i]+(1-a)*e;out[i]=e
    return pd.Series(out,index=s.index)


def simple_rsi(s,n=14):
    d=s.diff();g=d.clip(lower=0).rolling(n).mean();l=(-d.clip(upper=0)).rolling(n).mean()
    rs=g/l.replace(0,np.nan);out=100-100/(1+rs)
    out=out.where(l>0,100.0);out=out.where(~((l==0)&(g==0)),50.0)
    return out


def atr_abs(df,n=14):
    pc=df["close"].shift(1)
    tr=pd.concat([(df["high"]-df["low"]).abs(),(df["high"]-pc).abs(),(df["low"]-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()


def macd_hist(s):
    f=ema_seeded(s,12);sl=ema_seeded(s,26);m=f-sl
    valid=m.dropna();sig=ema_seeded(valid,9).reindex(s.index)
    return m-sig


def build_features(df15):
    d=df15.copy();d1=resample(d,"1h");d4=resample(d,"4h")
    d["taker_ratio"]=d["taker_quote"]/d["quote_volume"].replace(0,np.nan)
    d["taker3"]=d["taker_ratio"].rolling(3).mean()
    d["rvol"]=d["quote_volume"]/d["quote_volume"].shift(1).rolling(20).mean().replace(0,np.nan)
    d["qv24"]=d["quote_volume"].rolling(96).sum()

    c1=d1["close"]
    d1["ema20"]=ema_seeded(c1,20);d1["ema50"]=ema_seeded(c1,50);d1["ema200"]=ema_seeded(c1,200)
    d1["rsi"]=simple_rsi(c1);d1["rsi_prev3"]=d1["rsi"].shift(3)
    d1["macd"]=macd_hist(c1);d1["macd_prev"]=d1["macd"].shift(1)
    d1["atr_abs"]=atr_abs(d1);d1["atr_pct"]=d1["atr_abs"]/d1["close"].replace(0,np.nan)
    mean=c1.rolling(20).mean();sd=c1.rolling(20).std(ddof=0);d1["bw"]=4*sd/mean.replace(0,np.nan)

    c4=d4["close"];d4["ema50_4h"]=ema_seeded(c4,50);d4["ema200_4h"]=ema_seeded(c4,200)
    one=d1[["close","ema20","ema50","ema200","rsi","rsi_prev3","macd","macd_prev","atr_abs","atr_pct","bw"]].reindex(d.index,method="ffill")
    four=d4[["ema50_4h","ema200_4h"]].reindex(d.index,method="ffill")
    for c in one.columns:d["h1_"+c]=one[c]
    for c in four.columns:d[c]=four[c]

    ec=CFG["entry"]
    trend=(8*(d["ema50_4h"]>d["ema200_4h"]).astype(int)
           +6*(d["h1_ema20"]>d["h1_ema50"]).astype(int)
           +6*(d["h1_ema50"]>d["h1_ema200"]).astype(int)
           +5*(d["h1_close"]>d["h1_ema20"]).astype(int))
    mom=(8*d["h1_rsi"].between(float(ec["rsi_min"]),float(ec["rsi_max"])).astype(int)
         +4*(d["h1_rsi"]>d["h1_rsi_prev3"]).astype(int)
         +4*(d["h1_macd"]>0).astype(int)
         +4*(d["h1_macd"]>d["h1_macd_prev"]).astype(int))
    flow=(15*(d["taker_ratio"]>=float(ec["min_taker_buy_ratio"])).astype(int)
          +5*(d["taker3"]>=0.54).astype(int)
          +10*(d["rvol"]>=float(ec["min_relative_quote_volume"])).astype(int))
    vol=(6*d["h1_atr_pct"].between(float(ec["atr_pct_min"]),float(ec["atr_pct_max"])).astype(int)
         +4*d["h1_bw"].between(0.01,0.12).astype(int))
    liq=10*(d["qv24"]>=float(CFG["universe"]["min_quote_volume_24h"])).astype(int)+5
    d["score"]=trend+mom+flow+vol+liq
    return d


def btc_ok_series(btc15):
    f=build_features(btc15)
    d1=resample(btc15,"1h");d1["ret1h"]=d1["close"].pct_change()
    ret=d1["ret1h"].reindex(f.index,method="ffill")
    return ((f["h1_ema20"]>f["h1_ema50"])&(f["ema50_4h"]>f["ema200_4h"])&
            (ret>float(CFG["entry"]["btc_max_1h_drop"]))).fillna(False)


def parameter_grid():
    keys=["scoreMin","takerMin","taker3Min","rvolMin","rsiMax","stopMult","rewardRisk"]
    vals=itertools.product(SCORE_MIN,TAKER_MIN,TAKER3_MIN,RVOL_MIN,RSI_MAX,STOP_MULT,REWARD_RISK)
    return [dict(zip(keys,x)) for x in vals]


def base_candidate_mask(f,btc_ok,start,end):
    ec=CFG["entry"]
    time_mask=(f.index>=start)&(f.index<end)
    hard=(
        (f["taker_ratio"]>=float(ec["hard_taker_floor"]))
        & (f["rvol"]>=float(ec["hard_relative_volume_floor"]))
        & (f["h1_rsi"]<=float(ec["rsi_veto"]))
        & (f["h1_atr_pct"]<=float(ec["atr_pct_veto"]))
        & (f["qv24"]>=float(CFG["universe"]["min_quote_volume_24h"]))
    )
    return time_mask&hard.fillna(False)&btc_ok.reindex(f.index,method="ffill").fillna(False)&(f["score"]>=90)


def qualifies(row,p):
    return (
        row["score"]>=p["scoreMin"] and
        row["taker_ratio"]>=p["takerMin"] and
        row["taker3"]>=p["taker3Min"] and
        row["rvol"]>=p["rvolMin"] and
        float(CFG["entry"]["rsi_min"])<=row["h1_rsi"]<=p["rsiMax"]
    )


def simulate_one(f,i,stop_mult,rr):
    if i+1>=len(f):return None
    atr_pct=float(f["h1_atr_pct"].iloc[i])
    if not math.isfinite(atr_pct) or atr_pct<=0:return None
    entry_i=i+1
    entry=float(f["open"].iloc[entry_i])*(1+SLIP)
    stop_frac=max(float(CFG["risk"]["min_stop_fraction"]),
                  min(float(CFG["risk"]["max_stop_fraction"]),atr_pct*float(stop_mult)))
    stop=entry*(1-stop_frac);target=entry*(1+stop_frac*float(rr))
    end=min(len(f)-1,entry_i+MAX_FORWARD_BARS)
    exit_px=None;reason=None;exit_i=end
    for j in range(entry_i,end+1):
        lo=float(f["low"].iloc[j]);hi=float(f["high"].iloc[j])
        if lo<=stop:
            exit_px=stop;reason="STOP";exit_i=j;break
        if hi>=target:
            exit_px=target;reason="TARGET";exit_i=j;break
    if exit_px is None:
        exit_px=float(f["close"].iloc[end]);reason="TIME";exit_i=end
    buy_cost=entry*(1+FEE)
    sell_net=exit_px*(1-SLIP)*(1-FEE)
    pnl=sell_net-buy_cost
    return {"pnl":float(pnl),"profitable":bool(pnl>0),"exit_i":int(exit_i),"reason":reason}


def build_records(symbol,f,btc_ok,start,end):
    mask=base_candidate_mask(f,btc_ok,start,end)
    idx=np.flatnonzero(mask.to_numpy(bool))
    out=[]
    risk_pairs=list(itertools.product(STOP_MULT,REWARD_RISK))
    for i in idx:
        row=f.iloc[i]
        rec={
            "symbol":symbol,"i":int(i),"ts":f.index[i],
            "score":float(row["score"]),"taker_ratio":float(row["taker_ratio"]),
            "taker3":float(row["taker3"]),"rvol":float(row["rvol"]),
            "h1_rsi":float(row["h1_rsi"]),"outcomes":{},
        }
        for sm,rr in risk_pairs:
            x=simulate_one(f,int(i),sm,rr)
            if x is not None:rec["outcomes"][f"{sm}:{rr}"]=x
        out.append(rec)
    return out


def evaluate(records,p):
    selected=[r for r in records if qualifies(r,p)]
    selected.sort(key=lambda r:(r["symbol"],r["i"]))
    last_exit={}
    outcomes=[]
    key=f"{p['stopMult']}:{p['rewardRisk']}"
    for r in selected:
        if r["i"]<=last_exit.get(r["symbol"],-1):continue
        x=r["outcomes"].get(key)
        if not x:continue
        outcomes.append(x["pnl"])
        last_exit[r["symbol"]]=x["exit_i"]
    n=len(outcomes);wins=sum(x>0 for x in outcomes);losses=n-wins
    gp=sum(x for x in outcomes if x>0);gl=-sum(x for x in outcomes if x<0)
    pf=(gp/gl) if gl>0 else (999.0 if gp>0 else 0.0)
    return {
        "trades":n,"wins":wins,"losses":losses,"winRate":wins/n if n else 0.0,
        "netPnlPerUnit":float(sum(outcomes)),"expectancyPerUnit":float(sum(outcomes)/n) if n else 0.0,
        "profitFactor":float(pf),
    }


def wilson_lower(wins,n,z=1.96):
    if n<=0:return 0.0
    ph=wins/n;den=1+z*z/n
    return (ph+z*z/(2*n)-z*math.sqrt(ph*(1-ph)/n+z*z/(4*n*n)))/den


def rank_key(m):
    return (wilson_lower(m["wins"],m["trades"]),m["winRate"],m["profitFactor"],m["expectancyPerUnit"],m["trades"])


def main():
    now=pd.Timestamp.now(tz="UTC")
    cal_end=now-pd.Timedelta(days=DEV_EXCLUDE_DAYS)
    cal_start=cal_end-pd.Timedelta(days=CAL_WINDOW_DAYS)
    train_end=cal_start+pd.Timedelta(days=TRAIN_DAYS)
    val_end=train_end+pd.Timedelta(days=VALIDATION_DAYS)
    hold_end=val_end+pd.Timedelta(days=HOLDOUT_DAYS)

    uni=universe();symbols=[x["symbol"] for x in uni]
    data={};failures={}
    with ThreadPoolExecutor(max_workers=6) as ex:
        fut={ex.submit(fetch_15m,s):s for s in list(dict.fromkeys(["BTCUSDT"]+symbols))}
        for future in as_completed(fut):
            s=fut[future]
            try:data[s]=future.result()
            except Exception as exc:failures[s]=str(exc)
    if "BTCUSDT" not in data:raise RuntimeError("BTC data unavailable")
    btc_ok=btc_ok_series(data["BTCUSDT"])

    feature_data={}
    for s in symbols:
        if s in data:feature_data[s]=build_features(data[s])

    records={"train":[],"validation":[],"holdout":[]}
    windows={"train":(cal_start,train_end),"validation":(train_end,val_end),"holdout":(val_end,hold_end)}
    for s,f in feature_data.items():
        for name,(a,b) in windows.items():
            records[name].extend(build_records(s,f,btc_ok,a,b))

    grid=parameter_grid()
    train_rows=[]
    for p in grid:
        m=evaluate(records["train"],p)
        if m["trades"]>=MIN_TRAIN_TRADES and m["netPnlPerUnit"]>0 and m["profitFactor"]>1:
            train_rows.append({"params":p,"metrics":m,"wilsonLower":wilson_lower(m["wins"],m["trades"])})
    train_rows.sort(key=lambda x:rank_key(x["metrics"]),reverse=True)
    shortlist=train_rows[:40]

    validation_rows=[]
    for x in shortlist:
        m=evaluate(records["validation"],x["params"])
        if m["trades"]>=MIN_VALIDATION_TRADES and m["netPnlPerUnit"]>0 and m["profitFactor"]>1:
            validation_rows.append({"params":x["params"],"train":x["metrics"],"metrics":m,
                                    "wilsonLower":wilson_lower(m["wins"],m["trades"])})
    validation_rows.sort(key=lambda x:rank_key(x["metrics"]),reverse=True)

    selected=validation_rows[0] if validation_rows else None
    holdout=evaluate(records["holdout"],selected["params"]) if selected else {
        "trades":0,"wins":0,"losses":0,"winRate":0.0,"netPnlPerUnit":0.0,
        "expectancyPerUnit":0.0,"profitFactor":0.0,
    }
    statistical_claim=bool(
        selected and holdout["trades"]>=MIN_HOLDOUT_CLAIM_TRADES and
        holdout["winRate"]>=0.99 and holdout["netPnlPerUnit"]>0 and holdout["profitFactor"]>1
    )
    universe_integrity=bool(POINT_IN_TIME_UNIVERSE and DELISTED_COVERAGE)
    claim=bool(statistical_claim and universe_integrity)
    claim_blocked_reasons=[]
    if not statistical_claim:
        claim_blocked_reasons.append("STATISTICAL_99_PERCENT_HOLDOUT_NOT_MET")
    if not POINT_IN_TIME_UNIVERSE:
        claim_blocked_reasons.append("POINT_IN_TIME_UNIVERSE_NOT_USED")
    if not DELISTED_COVERAGE:
        claim_blocked_reasons.append("DELISTED_SYMBOL_COVERAGE_NOT_USED")

    # Also report the absolute highest holdout win rate among all configs only
    # as a diagnostic. It is NOT eligible for selection because that would peek.
    diagnostic_best=None
    for p in grid:
        m=evaluate(records["holdout"],p)
        if m["trades"]>=10 and m["netPnlPerUnit"]>0:
            row={"params":p,"metrics":m}
            if diagnostic_best is None or rank_key(m)>rank_key(diagnostic_best["metrics"]):
                diagnostic_best=row

    report={
        "engine":"INDICATOR_ONLY_PRECISION_CALIBRATION_V1",
        "authorization":"RESEARCH_ONLY","liveTrading":False,"runtimeChanged":False,
        "target":{"winRate":0.99,"minimumHoldoutTrades":MIN_HOLDOUT_CLAIM_TRADES,"requiresPositiveNet":True},
        "windows":{"train":[cal_start.isoformat(),train_end.isoformat()],
                   "validation":[train_end.isoformat(),val_end.isoformat()],
                   "holdout":[val_end.isoformat(),hold_end.isoformat()],
                   "recentExcluded":[cal_end.isoformat(),now.isoformat()]},
        "gridSize":len(grid),"symbolsRequested":symbols,"symbolsAudited":list(feature_data),"dataFailures":failures,
        "baseRecordCounts":{k:len(v) for k,v in records.items()},
        "trainQualifiedConfigs":len(train_rows),"validationQualifiedConfigs":len(validation_rows),
        "selected":selected,"holdout":holdout,
        "statisticalClaimBeforeUniverseIntegrity":statistical_claim,
        "universeIntegrity":{
            "pointInTime":POINT_IN_TIME_UNIVERSE,
            "delistedCoverage":DELISTED_COVERAGE,
            "productionGrade":universe_integrity,
        },
        "claimSupported":claim,
        "claimBlockedReasons":claim_blocked_reasons,
        "diagnosticBestHoldoutPeekingNotEligible":diagnostic_best,
        "notes":[
            "The final holdout is not used to select parameters.",
            "Recent 180 days are excluded because they were already inspected in the prior audit.",
            "Current-universe historical testing has survivorship bias and can never set claimSupported=true.",
            "Historical spread is unavailable; this calibration does not add a spread penalty, which favors the strategy.",
            "Profitability includes configured buy/sell fee and slippage assumptions.",
            "A 99% claim is accepted only with >=100 profitable-trade observations on untouched holdout and positive net PnL."
        ],
        "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(report,indent=2,default=str))
    print(json.dumps({
        "gridSize":len(grid),"records":report["baseRecordCounts"],
        "trainQualifiedConfigs":len(train_rows),"validationQualifiedConfigs":len(validation_rows),
        "selected":selected,"holdout":holdout,"claimSupported":claim,
        "diagnosticBestHoldoutPeekingNotEligible":diagnostic_best,
        "symbolsAudited":len(feature_data),"failures":failures,
    },indent=2,default=str))


if __name__=="__main__":
    main()
