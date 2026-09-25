#!/usr/bin/env python3
"""Independent historical audit for the scheduled INDICATOR_ONLY_V1 paper runtime.

Purpose:
- detect whether the current 90/100 indicator gate has anything close to a 99% hit rate
- use the current runtime formulas as closely as practical without historical L2/bookTicker
- fail conservatively on look-ahead: signals use closed 15m/1h/4h bars and enter next 15m open
- report both pessimistic and optimistic same-bar stop/target ordering

Historical bid/ask spread snapshots are unavailable from Spot klines. The audit therefore
grants the liquidity spread points and does not apply the spread veto. That makes the
historical hit-rate test FAVORABLE to the runtime; it must not be treated as a live guarantee.
"""

from __future__ import annotations

import datetime as dt
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
DAYS=180
MAX_SYMBOLS=int(CFG["universe"]["max_symbols"])
OUT=pathlib.Path("validation/indicator-only-winrate-audit-latest.json")
FEE=float(CFG["risk"]["fee_rate"])
SLIP=float(CFG["risk"]["slippage_rate"])
MAX_FORWARD_BARS=7*24*4  # 7 days; unresolved trades are reported separately.


def api(path):
    req=urllib.request.Request(BASE+path,headers={"User-Agent":"tst-indicator-winrate-audit/1.0"})
    with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)


def universe():
    info=api("/api/v3/exchangeInfo")
    tick={x["symbol"]:x for x in api("/api/v3/ticker/24hr")}
    stable={"USDC","FDUSD","TUSD","USDP","DAI","BUSD","USD1","RLUSD","USDE","EUR","AEUR","TRY","BRL","GBP","AUD"}
    rows=[]
    for s in info.get("symbols",[]):
        if s.get("status")!="TRADING" or s.get("quoteAsset")!="USDT" or not s.get("isSpotTradingAllowed",True):continue
        base=s.get("baseAsset","")
        if not base or base in stable or base.endswith(("UP","DOWN","BULL","BEAR")):continue
        t=tick.get(s["symbol"],{})
        qv=float(t.get("quoteVolume") or 0); px=float(t.get("lastPrice") or 0)
        if px>0 and qv>=float(CFG["universe"]["min_quote_volume_24h"]):
            rows.append({"symbol":s["symbol"],"quoteVolume24h":qv})
    rows.sort(key=lambda x:x["quoteVolume24h"],reverse=True)
    return rows[:MAX_SYMBOLS]


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
        time.sleep(.005)
    if len(rows)<3000:raise RuntimeError(f"{symbol}: insufficient 15m bars {len(rows)}")
    d=pd.DataFrame(rows,columns=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"])
    for c in ["open","high","low","close","volume","quote_volume","taker_quote"]:d[c]=pd.to_numeric(d[c],errors="coerce")
    d["ts_close"]=pd.to_datetime(d["close_time"],unit="ms",utc=True)
    return d.set_index("ts_close")[["open","high","low","close","volume","quote_volume","taker_quote"]].dropna()


def resample(df, rule):
    return df.resample(rule,label="right",closed="right").agg({
        "open":"first","high":"max","low":"min","close":"last",
        "volume":"sum","quote_volume":"sum","taker_quote":"sum",
    }).dropna()


def ema_seeded(s,n):
    a=2/(n+1)
    x=s.to_numpy(dtype=float)
    out=np.full(len(x),np.nan)
    if len(x)<n:return pd.Series(out,index=s.index)
    e=float(np.mean(x[:n])); out[n-1]=e
    for i in range(n,len(x)):
        e=a*x[i]+(1-a)*e; out[i]=e
    return pd.Series(out,index=s.index)


def simple_rsi(s,n=14):
    d=s.diff()
    gains=d.clip(lower=0).rolling(n).mean()
    losses=(-d.clip(upper=0)).rolling(n).mean()
    rs=gains/losses.replace(0,np.nan)
    out=100-100/(1+rs)
    out=out.where(losses>0,100.0)
    out=out.where(~((losses==0)&(gains==0)),50.0)
    return out


def atr_abs(df,n=14):
    pc=df["close"].shift(1)
    tr=pd.concat([(df["high"]-df["low"]).abs(),(df["high"]-pc).abs(),(df["low"]-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()


def macd_hist(s):
    fast=ema_seeded(s,12); slow=ema_seeded(s,26)
    macd=fast-slow
    valid=macd.dropna()
    sig=ema_seeded(valid,9)
    aligned=sig.reindex(s.index)
    return macd-aligned


def build_features(df15):
    d15=df15.copy()
    d1=resample(d15,"1h")
    d4=resample(d15,"4h")

    # 15m flow
    d15["taker_ratio"]=d15["taker_quote"]/d15["quote_volume"].replace(0,np.nan)
    d15["taker_ratio_3"]=d15["taker_ratio"].rolling(3).mean()
    d15["rvol"]=d15["quote_volume"]/d15["quote_volume"].shift(1).rolling(20).mean().replace(0,np.nan)
    d15["qv24"]=d15["quote_volume"].rolling(96).sum()

    # 1h
    c1=d1["close"]
    d1["ema20"]=ema_seeded(c1,20); d1["ema50"]=ema_seeded(c1,50); d1["ema200"]=ema_seeded(c1,200)
    d1["rsi"]=simple_rsi(c1,14); d1["rsi_prev3"]=d1["rsi"].shift(3)
    d1["macd_hist"]=macd_hist(c1); d1["macd_hist_prev"]=d1["macd_hist"].shift(1)
    d1["atr_abs"]=atr_abs(d1,14); d1["atr_pct"]=d1["atr_abs"]/d1["close"].replace(0,np.nan)
    mean=c1.rolling(20).mean(); sd=c1.rolling(20).std(ddof=0)
    d1["bb_width"]=4*sd/mean.replace(0,np.nan)

    # 4h
    c4=d4["close"]
    d4["ema50_4h"]=ema_seeded(c4,50); d4["ema200_4h"]=ema_seeded(c4,200)

    # Bring only COMPLETED higher-timeframe bars forward to each 15m close.
    one=d1[["close","ema20","ema50","ema200","rsi","rsi_prev3","macd_hist","macd_hist_prev","atr_abs","atr_pct","bb_width"]].reindex(d15.index,method="ffill")
    four=d4[["ema50_4h","ema200_4h"]].reindex(d15.index,method="ffill")
    for c in one.columns:d15["h1_"+c]=one[c]
    for c in four.columns:d15[c]=four[c]
    return d15


def btc_regime_series(btc15):
    f=build_features(btc15)
    ret1=f["h1_close"]/f["h1_close"].shift(4)-1  # previous completed 1h close proxy at 15m grid
    # Runtime tests the latest 1h bar return, not 15m return.
    d1=resample(btc15,"1h")
    d1["ret1h"]=d1["close"].pct_change()
    ret=d1["ret1h"].reindex(f.index,method="ffill")
    ok=(f["h1_ema20"]>f["h1_ema50"])&(f["ema50_4h"]>f["ema200_4h"])&(ret>float(CFG["entry"]["btc_max_1h_drop"]))
    return ok.fillna(False)


def score_frame(f, btc_ok):
    ec=CFG["entry"]
    g_trend=(8*(f["ema50_4h"]>f["ema200_4h"]).astype(int)
             +6*(f["h1_ema20"]>f["h1_ema50"]).astype(int)
             +6*(f["h1_ema50"]>f["h1_ema200"]).astype(int)
             +5*(f["h1_close"]>f["h1_ema20"]).astype(int))
    g_mom=(8*f["h1_rsi"].between(float(ec["rsi_min"]),float(ec["rsi_max"])).astype(int)
           +4*(f["h1_rsi"]>f["h1_rsi_prev3"]).astype(int)
           +4*(f["h1_macd_hist"]>0).astype(int)
           +4*(f["h1_macd_hist"]>f["h1_macd_hist_prev"]).astype(int))
    g_flow=(15*(f["taker_ratio"]>=float(ec["min_taker_buy_ratio"])).astype(int)
            +5*(f["taker_ratio_3"]>=0.54).astype(int)
            +10*(f["rvol"]>=float(ec["min_relative_quote_volume"])).astype(int))
    g_vol=(6*f["h1_atr_pct"].between(float(ec["atr_pct_min"]),float(ec["atr_pct_max"])).astype(int)
           +4*f["h1_bb_width"].between(0.01,0.12).astype(int))
    # Historical spread is unavailable: grant all 5 spread points (optimistic).
    g_liq=10*(f["qv24"]>=float(CFG["universe"]["min_quote_volume_24h"])).astype(int)+5
    score=g_trend+g_mom+g_flow+g_vol+g_liq
    veto=(f["taker_ratio"]<float(ec["hard_taker_floor"]))|(f["rvol"]<float(ec["hard_relative_volume_floor"]))|(f["h1_rsi"]>float(ec["rsi_veto"]))|(f["h1_atr_pct"]>float(ec["atr_pct_veto"]))|(f["qv24"]<float(CFG["universe"]["min_quote_volume_24h"]))
    eligible=(score>=float(ec["min_score"]))&(~veto.fillna(True))&btc_ok.reindex(f.index,method="ffill").fillna(False)
    return score,eligible


def evaluate_signals(symbol,f,eligible):
    pessimistic=[]; optimistic=[]; unresolved=0
    indices=np.flatnonzero(eligible.to_numpy(dtype=bool))
    last_exit=-1
    # Runtime cannot open a second position in the same symbol while one is open.
    for sig_i in indices:
        if sig_i<=last_exit or sig_i+1>=len(f):continue
        atr_pct=float(f["h1_atr_pct"].iloc[sig_i])
        if not math.isfinite(atr_pct) or atr_pct<=0:continue
        entry_i=sig_i+1
        entry=float(f["open"].iloc[entry_i])*(1+SLIP)
        stop_frac=max(float(CFG["risk"]["min_stop_fraction"]),min(float(CFG["risk"]["max_stop_fraction"]),atr_pct*float(CFG["risk"]["stop_atr_multiplier"])))
        stop=entry*(1-stop_frac); target=entry*(1+stop_frac*float(CFG["risk"]["reward_risk"]))
        end=min(len(f)-1,entry_i+MAX_FORWARD_BARS)
        p=None;o=None;exit_i=end
        for j in range(entry_i,end+1):
            lo=float(f["low"].iloc[j]); hi=float(f["high"].iloc[j])
            hit_s=lo<=stop; hit_t=hi>=target
            if p is None:
                if hit_s and hit_t:p="LOSS"  # pessimistic same-bar ordering
                elif hit_s:p="LOSS"
                elif hit_t:p="WIN"
            if o is None:
                if hit_s and hit_t:o="WIN"   # optimistic upper bound
                elif hit_t:o="WIN"
                elif hit_s:o="LOSS"
            if p is not None and o is not None:
                exit_i=j;break
        if p is None or o is None:
            unresolved+=1
            # Do not manufacture an outcome.
            last_exit=end
            continue
        pessimistic.append(p); optimistic.append(o); last_exit=exit_i
    return pessimistic,optimistic,unresolved


def stats(outcomes):
    n=len(outcomes); wins=sum(x=="WIN" for x in outcomes)
    return {"resolvedTrades":n,"wins":wins,"losses":n-wins,"winRate":wins/n if n else 0.0}


def main():
    uni=universe()
    symbols=[x["symbol"] for x in uni]
    data={};failures={}
    all_fetch=list(dict.fromkeys(["BTCUSDT"]+symbols))
    with ThreadPoolExecutor(max_workers=6) as ex:
        fut={ex.submit(fetch_15m,s):s for s in all_fetch}
        for x in as_completed(fut):
            s=fut[x]
            try:data[s]=x.result()
            except Exception as exc:failures[s]=str(exc)
    if "BTCUSDT" not in data:raise RuntimeError("BTC history unavailable")
    btc_ok=btc_regime_series(data["BTCUSDT"])

    pess=[]; opt=[]; unresolved=0; per={}
    for symbol in symbols:
        if symbol not in data:continue
        f=build_features(data[symbol])
        score,eligible=score_frame(f,btc_ok)
        p,o,u=evaluate_signals(symbol,f,eligible)
        pess+=p;opt+=o;unresolved+=u
        per[symbol]={"pessimistic":stats(p),"optimistic":stats(o),"unresolved":u,
                     "signalBars":int(eligible.sum()),"maxScore":float(score.max())}

    pstats=stats(pess); ostats=stats(opt)
    report={
        "engine":"INDICATOR_ONLY_V1_HISTORICAL_AUDIT",
        "runtimeEngine":CFG["engine"],
        "days":DAYS,
        "symbolsRequested":symbols,
        "symbolsAudited":[s for s in symbols if s in data],
        "dataFailures":failures,
        "scoreThreshold":CFG["entry"]["min_score"],
        "targetClaimWinRate":0.99,
        "pessimistic":pstats,
        "optimisticUpperBound":ostats,
        "unresolvedWithin7d":unresolved,
        "perSymbol":per,
        "assumptions":{
            "entry":"next 15m open plus configured slippage",
            "stopTarget":"current ATR stop geometry and 2R target",
            "sameBarPessimistic":"stop first",
            "sameBarOptimistic":"target first",
            "feesIncludedInGeometry":False,
            "historicalSpread":"unavailable; spread gate assumed PASS and all spread score points granted",
            "breakevenTrailing":"not modeled; audit measures initial stop vs 2R target hit rate",
            "universeBias":"current liquid universe applied historically",
        },
        "claimSupported":bool(ostats["resolvedTrades"]>=100 and ostats["winRate"]>=0.99),
        "authorization":"AUDIT_ONLY",
        "liveTrading":False,
        "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(report,indent=2))
    print(json.dumps({
        "pessimistic":pstats,
        "optimisticUpperBound":ostats,
        "unresolvedWithin7d":unresolved,
        "claimSupported":report["claimSupported"],
        "symbolsAudited":len(report["symbolsAudited"]),
        "failures":failures,
    },indent=2))


if __name__=="__main__":
    main()
