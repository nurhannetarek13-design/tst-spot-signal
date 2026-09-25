#!/usr/bin/env python3
"""Ultra-precision calibration V2 for the indicator-only paper engine.

This explores a precision-first risk geometry: stronger flow/RVOL filters,
wider bounded stops, and smaller targets. It uses an older period that predates
all previously inspected indicator-only audits.

No runtime settings are changed automatically.
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

import pandas as pd

import research.indicator_only_precision_calibration as base

CFG=base.CFG
BASE=base.BASE
DAYS=900
DEV_EXCLUDE_DAYS=540
CAL_WINDOW_DAYS=360
TRAIN_DAYS=180
VALIDATION_DAYS=90
HOLDOUT_DAYS=90
OUT=pathlib.Path("validation/indicator-only-ultra-precision-v2-latest.json")
FEE=base.FEE
SLIP=base.SLIP
MAX_FORWARD_BARS=base.MAX_FORWARD_BARS

SCORE_MIN=[95,100]
TAKER_MIN=[0.60,0.65,0.70,0.75]
TAKER3_MIN=[0.58,0.62,0.66]
RVOL_MIN=[1.5,2.0,3.0]
RSI_MAX=[62,66,70]
STOP_MULT=[1.5,2.0,2.5]
MIN_STOP=[0.02,0.025,0.03]
REWARD_RISK=[0.12,0.15,0.20,0.25,0.35]

MIN_TRAIN_TRADES=100
MIN_VALIDATION_TRADES=50
MIN_HOLDOUT_CLAIM_TRADES=100


def api(path):
    req=urllib.request.Request(BASE+path,headers={"User-Agent":"tst-ultra-precision-v2/1.0"})
    with urllib.request.urlopen(req,timeout=30) as r:
        return json.load(r)


def fetch_15m(symbol):
    end=int(time.time()*1000)
    start=end-DAYS*86400000
    rows=[]
    cur=start
    while cur<end:
        q=urllib.parse.urlencode({
            "symbol":symbol,"interval":"15m","limit":1000,
            "startTime":cur,"endTime":end
        })
        batch=api("/api/v3/klines?"+q)
        if not batch:
            break
        rows.extend(batch)
        nxt=int(batch[-1][0])+900000
        if nxt<=cur:
            break
        cur=nxt
        time.sleep(.004)
    if len(rows)<12000:
        raise RuntimeError(f"{symbol}: insufficient bars {len(rows)}")
    d=pd.DataFrame(rows,columns=[
        "open_time","open","high","low","close","volume","close_time",
        "quote_volume","trades","taker_base","taker_quote","ignore"
    ])
    for c in ["open","high","low","close","volume","quote_volume","taker_quote"]:
        d[c]=pd.to_numeric(d[c],errors="coerce")
    d["ts_close"]=pd.to_datetime(d["close_time"],unit="ms",utc=True)
    return d.set_index("ts_close")[
        ["open","high","low","close","volume","quote_volume","taker_quote"]
    ].dropna()


def parameter_grid():
    keys=[
        "scoreMin","takerMin","taker3Min","rvolMin","rsiMax",
        "stopMult","minStop","rewardRisk"
    ]
    vals=itertools.product(
        SCORE_MIN,TAKER_MIN,TAKER3_MIN,RVOL_MIN,RSI_MAX,
        STOP_MULT,MIN_STOP,REWARD_RISK
    )
    return [dict(zip(keys,x)) for x in vals]


def qualifies(row,p):
    return (
        row["score"]>=p["scoreMin"]
        and row["taker_ratio"]>=p["takerMin"]
        and row["taker3"]>=p["taker3Min"]
        and row["rvol"]>=p["rvolMin"]
        and float(CFG["entry"]["rsi_min"])<=row["h1_rsi"]<=p["rsiMax"]
    )


def simulate_one(f,i,stop_mult,min_stop,rr):
    if i+1>=len(f):
        return None
    atr_pct=float(f["h1_atr_pct"].iloc[i])
    if not math.isfinite(atr_pct) or atr_pct<=0:
        return None
    entry_i=i+1
    entry=float(f["open"].iloc[entry_i])*(1+SLIP)
    stop_frac=max(
        float(min_stop),
        min(float(CFG["risk"]["max_stop_fraction"]),atr_pct*float(stop_mult))
    )
    stop=entry*(1-stop_frac)
    target=entry*(1+stop_frac*float(rr))
    end=min(len(f)-1,entry_i+MAX_FORWARD_BARS)
    exit_px=None
    exit_i=end
    reason="TIME"
    for j in range(entry_i,end+1):
        lo=float(f["low"].iloc[j])
        hi=float(f["high"].iloc[j])
        if lo<=stop:
            exit_px=stop
            exit_i=j
            reason="STOP"
            break
        if hi>=target:
            exit_px=target
            exit_i=j
            reason="TARGET"
            break
    if exit_px is None:
        exit_px=float(f["close"].iloc[end])
    buy_cost=entry*(1+FEE)
    sell_net=exit_px*(1-SLIP)*(1-FEE)
    pnl=sell_net-buy_cost
    return {
        "pnl":float(pnl),
        "profitable":bool(pnl>0),
        "exit_i":int(exit_i),
        "reason":reason,
        "stopFraction":float(stop_frac),
        "targetFraction":float(stop_frac*float(rr)),
    }


def build_records(symbol,f,btc_ok,start,end):
    mask=base.base_candidate_mask(f,btc_ok,start,end)
    indices=[int(i) for i in list(mask.to_numpy().nonzero()[0])]
    risk_pairs=list(itertools.product(STOP_MULT,MIN_STOP,REWARD_RISK))
    out=[]
    for i in indices:
        row=f.iloc[i]
        rec={
            "symbol":symbol,
            "i":i,
            "score":float(row["score"]),
            "taker_ratio":float(row["taker_ratio"]),
            "taker3":float(row["taker3"]),
            "rvol":float(row["rvol"]),
            "h1_rsi":float(row["h1_rsi"]),
            "outcomes":{},
        }
        for sm,ms,rr in risk_pairs:
            x=simulate_one(f,i,sm,ms,rr)
            if x is not None:
                rec["outcomes"][f"{sm}:{ms}:{rr}"]=x
        out.append(rec)
    return out


def evaluate(records,p):
    selected=[r for r in records if qualifies(r,p)]
    selected.sort(key=lambda r:(r["symbol"],r["i"]))
    last_exit={}
    pnl=[]
    key=f"{p['stopMult']}:{p['minStop']}:{p['rewardRisk']}"
    reasons={}
    for r in selected:
        if r["i"]<=last_exit.get(r["symbol"],-1):
            continue
        x=r["outcomes"].get(key)
        if not x:
            continue
        pnl.append(x["pnl"])
        last_exit[r["symbol"]]=x["exit_i"]
        reasons[x["reason"]]=reasons.get(x["reason"],0)+1
    n=len(pnl)
    wins=sum(x>0 for x in pnl)
    gp=sum(x for x in pnl if x>0)
    gl=-sum(x for x in pnl if x<0)
    pf=gp/gl if gl>0 else (999.0 if gp>0 else 0.0)
    return {
        "trades":n,
        "wins":wins,
        "losses":n-wins,
        "winRate":wins/n if n else 0.0,
        "netPnlPerUnit":float(sum(pnl)),
        "expectancyPerUnit":float(sum(pnl)/n) if n else 0.0,
        "profitFactor":float(pf),
        "reasons":reasons,
    }


def rank_key(m):
    return (
        base.wilson_lower(m["wins"],m["trades"]),
        m["winRate"],
        m["profitFactor"],
        m["expectancyPerUnit"],
        m["trades"],
    )


def main():
    now=pd.Timestamp.now(tz="UTC")
    cal_end=now-pd.Timedelta(days=DEV_EXCLUDE_DAYS)
    cal_start=cal_end-pd.Timedelta(days=CAL_WINDOW_DAYS)
    train_end=cal_start+pd.Timedelta(days=TRAIN_DAYS)
    val_end=train_end+pd.Timedelta(days=VALIDATION_DAYS)
    hold_end=val_end+pd.Timedelta(days=HOLDOUT_DAYS)

    uni=base.universe()
    symbols=[x["symbol"] for x in uni]
    data={}
    failures={}
    fetch_symbols=list(dict.fromkeys(["BTCUSDT"]+symbols))
    with ThreadPoolExecutor(max_workers=6) as ex:
        fut={ex.submit(fetch_15m,s):s for s in fetch_symbols}
        for future in as_completed(fut):
            s=fut[future]
            try:
                data[s]=future.result()
            except Exception as exc:
                failures[s]=str(exc)
    if "BTCUSDT" not in data:
        raise RuntimeError("BTC data unavailable")

    btc_ok=base.btc_ok_series(data["BTCUSDT"])
    features={s:base.build_features(d) for s,d in data.items() if s in symbols}

    windows={
        "train":(cal_start,train_end),
        "validation":(train_end,val_end),
        "holdout":(val_end,hold_end),
    }
    records={k:[] for k in windows}
    symbols_with_window={k:[] for k in windows}
    for s,f in features.items():
        for name,(a,b) in windows.items():
            recs=build_records(s,f,btc_ok,a,b)
            records[name].extend(recs)
            if ((f.index>=a)&(f.index<b)).sum()>=1000:
                symbols_with_window[name].append(s)

    grid=parameter_grid()

    train_rows=[]
    for p in grid:
        m=evaluate(records["train"],p)
        if (
            m["trades"]>=MIN_TRAIN_TRADES
            and m["netPnlPerUnit"]>0
            and m["profitFactor"]>1
        ):
            train_rows.append({
                "params":p,
                "metrics":m,
                "wilsonLower":base.wilson_lower(m["wins"],m["trades"]),
            })
    train_rows.sort(key=lambda x:rank_key(x["metrics"]),reverse=True)
    shortlist=train_rows[:60]

    validation_rows=[]
    for x in shortlist:
        m=evaluate(records["validation"],x["params"])
        if (
            m["trades"]>=MIN_VALIDATION_TRADES
            and m["netPnlPerUnit"]>0
            and m["profitFactor"]>1
        ):
            validation_rows.append({
                "params":x["params"],
                "train":x["metrics"],
                "metrics":m,
                "wilsonLower":base.wilson_lower(m["wins"],m["trades"]),
            })
    validation_rows.sort(key=lambda x:rank_key(x["metrics"]),reverse=True)
    selected=validation_rows[0] if validation_rows else None

    holdout=evaluate(records["holdout"],selected["params"]) if selected else {
        "trades":0,"wins":0,"losses":0,"winRate":0.0,
        "netPnlPerUnit":0.0,"expectancyPerUnit":0.0,"profitFactor":0.0,
        "reasons":{},
    }
    claim=bool(
        selected
        and holdout["trades"]>=MIN_HOLDOUT_CLAIM_TRADES
        and holdout["winRate"]>=0.99
        and holdout["netPnlPerUnit"]>0
        and holdout["profitFactor"]>1
    )

    diagnostic_best=None
    diagnostic_max_win=None
    for p in grid:
        m=evaluate(records["holdout"],p)
        if m["trades"]>=10:
            row={"params":p,"metrics":m}
            if diagnostic_max_win is None or (
                m["winRate"],m["trades"],m["netPnlPerUnit"]
            )>(
                diagnostic_max_win["metrics"]["winRate"],
                diagnostic_max_win["metrics"]["trades"],
                diagnostic_max_win["metrics"]["netPnlPerUnit"],
            ):
                diagnostic_max_win=row
        if m["trades"]>=10 and m["netPnlPerUnit"]>0 and m["profitFactor"]>1:
            row={"params":p,"metrics":m}
            if diagnostic_best is None or rank_key(m)>rank_key(diagnostic_best["metrics"]):
                diagnostic_best=row

    report={
        "engine":"INDICATOR_ONLY_ULTRA_PRECISION_CALIBRATION_V2",
        "authorization":"RESEARCH_ONLY",
        "liveTrading":False,
        "runtimeChanged":False,
        "target":{
            "winRate":0.99,
            "minimumHoldoutTrades":MIN_HOLDOUT_CLAIM_TRADES,
            "requiresPositiveNet":True,
        },
        "windows":{
            "train":[cal_start.isoformat(),train_end.isoformat()],
            "validation":[train_end.isoformat(),val_end.isoformat()],
            "holdout":[val_end.isoformat(),hold_end.isoformat()],
            "allNewerHistoryExcluded":[cal_end.isoformat(),now.isoformat()],
        },
        "gridSize":len(grid),
        "symbolsRequested":symbols,
        "symbolsLoaded":list(features),
        "symbolsWithWindow":symbols_with_window,
        "dataFailures":failures,
        "baseRecordCounts":{k:len(v) for k,v in records.items()},
        "trainQualifiedConfigs":len(train_rows),
        "validationQualifiedConfigs":len(validation_rows),
        "selected":selected,
        "holdout":holdout,
        "claimSupported":claim,
        "diagnosticBestHoldoutPeekingNotEligible":diagnostic_best,
        "diagnosticMaxWinRateHoldoutPeekingNotEligible":diagnostic_max_win,
        "notes":[
            "This period predates the prior indicator-only audits.",
            "The final holdout is not used for parameter selection.",
            "Current-universe historical testing still has survivorship bias.",
            "Historical spread snapshots are unavailable, so no spread penalty is added.",
            "Fees and slippage are included in profitable-trade classification.",
            "Small targets are rejected automatically if costs make their net PnL non-positive.",
            "No setting is promoted to runtime unless train, validation and untouched holdout all support it.",
        ],
        "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(report,indent=2,default=str))
    print(json.dumps({
        "gridSize":len(grid),
        "baseRecordCounts":report["baseRecordCounts"],
        "symbolsWithWindow":{k:len(v) for k,v in symbols_with_window.items()},
        "trainQualifiedConfigs":len(train_rows),
        "validationQualifiedConfigs":len(validation_rows),
        "selected":selected,
        "holdout":holdout,
        "claimSupported":claim,
        "diagnosticBest":diagnostic_best,
        "diagnosticMaxWin":diagnostic_max_win,
        "failures":failures,
    },indent=2,default=str))


if __name__=="__main__":
    main()
