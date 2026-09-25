#!/usr/bin/env python3
"""Fixed-configuration confirmation on the most recent 180 days.

The configuration is frozen from the older-history diagnostic:
score>=95, taker>=0.60, taker3>=0.58, RVOL>=1.5, RSI<=70,
3% minimum stop, 1.5 ATR multiplier, target=0.15R.

No parameter search occurs here.
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

import pandas as pd

import research.indicator_only_precision_calibration as base
import research.indicator_only_ultra_precision_v2 as ultra

CFG=base.CFG
BASE=base.BASE
DAYS=180
OUT=pathlib.Path("validation/indicator-only-fixed-confirmation-latest.json")
PARAMS={
    "scoreMin":95,
    "takerMin":0.60,
    "taker3Min":0.58,
    "rvolMin":1.5,
    "rsiMax":70,
    "stopMult":1.5,
    "minStop":0.03,
    "rewardRisk":0.15,
}


def api(path):
    req=urllib.request.Request(BASE+path,headers={"User-Agent":"tst-fixed-confirmation/1.0"})
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
    if len(rows)<3000:
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


def evaluate_symbol(symbol,f,btc_ok,start,end):
    mask=base.base_candidate_mask(f,btc_ok,start,end)
    idx=[int(i) for i in list(mask.to_numpy().nonzero()[0])]
    last_exit=-1
    pnl=[]
    reasons={}
    accepted=0
    for i in idx:
        row=f.iloc[i]
        if not ultra.qualifies({
            "score":float(row["score"]),
            "taker_ratio":float(row["taker_ratio"]),
            "taker3":float(row["taker3"]),
            "rvol":float(row["rvol"]),
            "h1_rsi":float(row["h1_rsi"]),
        },PARAMS):
            continue
        if i<=last_exit:
            continue
        x=ultra.simulate_one(
            f,i,
            PARAMS["stopMult"],
            PARAMS["minStop"],
            PARAMS["rewardRisk"],
        )
        if x is None:
            continue
        accepted+=1
        pnl.append(float(x["pnl"]))
        last_exit=int(x["exit_i"])
        reasons[x["reason"]]=reasons.get(x["reason"],0)+1
    wins=sum(x>0 for x in pnl)
    gp=sum(x for x in pnl if x>0)
    gl=-sum(x for x in pnl if x<0)
    return {
        "trades":len(pnl),
        "wins":wins,
        "losses":len(pnl)-wins,
        "winRate":wins/len(pnl) if pnl else 0.0,
        "netPnlPerUnit":float(sum(pnl)),
        "expectancyPerUnit":float(sum(pnl)/len(pnl)) if pnl else 0.0,
        "profitFactor":float(gp/gl) if gl>0 else (999.0 if gp>0 else 0.0),
        "reasons":reasons,
        "rawAccepted":accepted,
    }


def aggregate(rows):
    trades=sum(x["trades"] for x in rows.values())
    wins=sum(x["wins"] for x in rows.values())
    losses=sum(x["losses"] for x in rows.values())
    net=sum(x["netPnlPerUnit"] for x in rows.values())
    gp=0.0
    gl=0.0
    # Recover PF approximately from each symbol's PF/net is not valid, so
    # report aggregate win/net and leave PF to symbol rows.
    return {
        "trades":trades,
        "wins":wins,
        "losses":losses,
        "winRate":wins/trades if trades else 0.0,
        "netPnlPerUnit":float(net),
    }


def main():
    uni=base.universe()
    symbols=[x["symbol"] for x in uni]
    data={}
    failures={}
    with ThreadPoolExecutor(max_workers=6) as ex:
        fut={ex.submit(fetch_15m,s):s for s in list(dict.fromkeys(["BTCUSDT"]+symbols))}
        for future in as_completed(fut):
            s=fut[future]
            try:
                data[s]=future.result()
            except Exception as exc:
                failures[s]=str(exc)
    if "BTCUSDT" not in data:
        raise RuntimeError("BTC data unavailable")
    btc_ok=base.btc_ok_series(data["BTCUSDT"])
    start=pd.Timestamp.now(tz="UTC")-pd.Timedelta(days=DAYS)
    end=pd.Timestamp.now(tz="UTC")
    per={}
    for s in symbols:
        if s not in data:
            continue
        f=base.build_features(data[s])
        per[s]=evaluate_symbol(s,f,btc_ok,start,end)

    agg=aggregate(per)
    report={
        "engine":"INDICATOR_ONLY_FIXED_CONFIRMATION",
        "authorization":"RESEARCH_ONLY",
        "liveTrading":False,
        "runtimeChanged":False,
        "days":DAYS,
        "params":PARAMS,
        "symbolsRequested":symbols,
        "symbolsAudited":list(per),
        "dataFailures":failures,
        "aggregate":agg,
        "perSymbol":per,
        "observed99":bool(agg["trades"]>=100 and agg["winRate"]>=0.99 and agg["netPnlPerUnit"]>0),
        "notes":[
            "Configuration was frozen before this run; no search/tuning occurs here.",
            "Fees and slippage are included by the imported simulator.",
            "Historical spread snapshots are unavailable, so no spread penalty is added.",
            "Current-universe historical testing still has survivorship bias."
        ],
        "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(report,indent=2))
    print(json.dumps({
        "params":PARAMS,
        "aggregate":agg,
        "observed99":report["observed99"],
        "symbolsAudited":len(per),
        "failures":failures,
    },indent=2))


if __name__=="__main__":
    main()
