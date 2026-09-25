#!/usr/bin/env python3
"""Binance Spot historical microstructure replay for V3 execution validation.

Uses Tardis Binance Spot depthSnapshot/depth/bookTicker/aggTrade raw messages.
This validates replay integrity, microstructure features, and realistic quote-size
fills. It is RESEARCH_ONLY and cannot authorize live trading.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import subprocess
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from typing import Any

from research.tardis_l2_replay import parse_ordered_raw

BASE="https://api.tardis.dev/v1/data-feeds/binance"
AUTHORIZATION="RESEARCH_ONLY"
ROUNDTRIP_COST_BPS=28.0


def fetch_minute(symbol:str,date:str,offset:int)->str:
    filters=[
        {"channel":"depthSnapshot","symbols":[symbol.lower()]},
        {"channel":"depth","symbols":[symbol.lower()]},
        {"channel":"bookTicker","symbols":[symbol.lower()]},
        {"channel":"aggTrade","symbols":[symbol.lower()]},
    ]
    encoded=urllib.parse.quote(json.dumps(filters,separators=(",",":")),safe='[]{}":,')
    url=f"{BASE}?from={urllib.parse.quote(date,safe=':-TZ')}&filters={encoded}&offset={int(offset)}"
    p=subprocess.run(
        ["curl","--compressed","-sS","-g","--connect-timeout","15","--max-time","90",
         "--retry","3","--retry-all-errors","--retry-delay","2",url],
        capture_output=True,check=True,timeout=180,
    )
    return p.stdout.decode("utf-8","replace")


def fetch_window(symbol:str,date:str,start_offset:int,minutes:int,warmup_from_zero:bool=True)->str:
    first=0 if warmup_from_zero else start_offset
    parts=[]
    for off in range(first,start_offset+minutes):
        body=fetch_minute(symbol,date,off)
        if body: parts.append(body.rstrip("\n"))
    return "\n".join(parts)+("\n" if parts else "")


def ts_ms(row:dict[str,Any])->int|None:
    d=row["data"]
    for k in ("E","T"):
        if d.get(k) is not None:
            try:return int(d[k])
            except Exception:pass
    prefix=str(row.get("prefix") or "").rstrip("Z")
    try:return int(datetime.fromisoformat(prefix).replace(tzinfo=timezone.utc).timestamp()*1000)
    except Exception:return None


def apply_side(book:dict[float,float],rows)->tuple[float,float]:
    adds=cancels=0.0
    for p0,q0 in rows or []:
        p=float(p0);q=float(q0);prev=float(book.get(p,0.0))
        delta=q-prev
        if q<=0:book.pop(p,None)
        else:book[p]=q
        quote=abs(delta)*p
        if delta>0:adds+=quote
        elif delta<0:cancels+=quote
    return adds,cancels


def top(book:dict[float,float],bid:bool,n:int=5):
    return sorted(book.items(),key=lambda x:x[0],reverse=bid)[:n]


def book_metrics(bids,asks,n=5):
    bt=top(bids,True,n);at=top(asks,False,n)
    if not bt or not at:return None
    best_bid,bq=bt[0];best_ask,aq=at[0]
    if best_bid>=best_ask:return None
    bid_quote=sum(p*q for p,q in bt);ask_quote=sum(p*q for p,q in at);den=bid_quote+ask_quote
    mid=(best_bid+best_ask)/2
    micro=(best_ask*bq+best_bid*aq)/(bq+aq) if bq+aq>0 else mid
    return {
        "best_bid":best_bid,"best_ask":best_ask,"mid":mid,
        "spread_bps":(best_ask-best_bid)/mid*10000,
        "obi":bid_quote/den if den>0 else 0.5,
        "microprice_bias_bps":(micro/mid-1)*10000,
    }


def simulate_buy(asks:dict[float,float],quote_usdt:float=10.0):
    remaining=float(quote_usdt);spent=qty=0.0
    levels=top(asks,False,100)
    best=levels[0][0] if levels else None
    for p,q in levels:
        available=p*q;take=min(remaining,available)
        if take>0:
            spent+=take;qty+=take/p;remaining-=take
        if remaining<=1e-12:break
    fill_ratio=spent/quote_usdt if quote_usdt>0 else 0
    avg=spent/qty if qty>0 else None
    slip=(avg/best-1)*10000 if avg and best else None
    return {"fill_ratio":fill_ratio,"avg_price":avg,"best_ask":best,"slippage_bps":slip}


def minute_buckets(trades:list[dict[str,float]]):
    out={}
    for t in trades:
        m=int(t["ts"])//60000
        b=out.setdefault(m,{"quote":0.0,"buy":0.0,"sell":0.0,"count":0,"open":None,"high":None,"low":None,"close":None})
        p=float(t["price"]);q=float(t["quote"]);buy=bool(t["buy"])
        b["quote"]+=q;b["count"]+=1
        if buy:b["buy"]+=q
        else:b["sell"]+=q
        b["open"]=p if b["open"] is None else b["open"]
        b["high"]=p if b["high"] is None else max(b["high"],p)
        b["low"]=p if b["low"] is None else min(b["low"],p)
        b["close"]=p
    return out


def median(xs):
    x=sorted(v for v in xs if v is not None and math.isfinite(v))
    return statistics.median(x) if x else None


def atr_1m(closed):
    if len(closed)<15:return None
    vals=[]
    for i in range(len(closed)-14,len(closed)):
        prev=closed[i-1]["close"];x=closed[i]
        vals.append(max(x["high"]-x["low"],abs(x["high"]-prev),abs(x["low"]-prev)))
    return sum(vals)/len(vals)


def historical_context(trades,ts):
    buckets=minute_buckets([t for t in trades if t["ts"]<=ts])
    keys=sorted(buckets)
    current_min=ts//60000
    closed=[buckets[k] for k in keys if k<current_min]
    if len(closed)<21:return {"ready":False}
    last20=closed[-20:]
    latest=closed[-1]
    qbase=median([x["quote"] for x in closed[-21:-1]])
    nbase=median([x["count"] for x in closed[-21:-1]])
    rvol=latest["quote"]/qbase if qbase else None
    trade_accel=latest["count"]/nbase if nbase else None
    takers=[]
    for x in closed[-3:]:
        den=x["buy"]+x["sell"];takers.append(x["buy"]/den if den>0 else None)
    taker_rising=all(v is not None for v in takers) and takers[0]<takers[1]<takers[2]
    cvd3=sum(x["buy"]-x["sell"] for x in closed[-3:])
    atr=atr_1m(closed)
    resistance=max(x["high"] for x in closed[-21:-1])
    price=latest["close"]
    prox=(resistance-price)/atr if atr and atr>0 else None
    # Session VWAP over closed trade buckets, quote/base volume approximated from trades.
    day=current_min//1440
    day_trades=[t for t in trades if t["ts"]<=ts and (t["ts"]//60000)//1440==day]
    base_vol=sum(t["quote"]/t["price"] for t in day_trades if t["price"]>0)
    vwap=sum(t["quote"] for t in day_trades)/base_vol if base_vol>0 else None
    vwap_dist=(price-vwap)/atr if vwap and atr else None
    return {
        "ready":True,"rvol_1m":rvol,"trade_accel":trade_accel,"taker_last3":takers,
        "taker_rising":taker_rising,"taker_latest":takers[-1],"cvd3_quote":cvd3,
        "atr_1m":atr,"breakout_distance_atr":prox,"vwap":vwap,"vwap_distance_atr":vwap_dist,
    }


def replay(rows:list[dict[str,Any]],symbol:str,eval_start_ms:int,eval_end_ms:int,quote_usdt:float=10.0):
    snapshots=[r for r in rows if "lastUpdateId" in r["data"] and "bids" in r["data"] and "asks" in r["data"]]
    if not snapshots:return {"status":"NO_SNAPSHOT","canonicalReplayReady":False}
    sr=snapshots[0];sid=int(sr["data"]["lastUpdateId"]);line=int(sr["line"])
    bids={float(p):float(q) for p,q in sr["data"]["bids"] if float(q)>0}
    asks={float(p):float(q) for p,q in sr["data"]["asks"] if float(q)>0}
    last=sid;bridged=False;gaps=0;events=0;ticker_comp=ticker_exact=0
    ticker_by_u={}
    trades=[]
    samples=[]
    candidates=[]
    flow=deque()

    for r in rows:
        if int(r["line"])<=line:continue
        d=r["data"];ts=ts_ms(r)
        if ts is None:continue

        is_ticker={"u","b","B","a","A"}.issubset(d)
        if is_ticker:
            ticker_by_u[int(d["u"])]=(float(d["b"]),float(d["a"]))
            continue

        if "a" in d and "p" in d and "q" in d and "m" in d:  # aggTrade
            p=float(d["p"]);q=float(d["q"]);buy=not bool(d["m"])
            trades.append({"ts":ts,"price":p,"quote":p*q,"buy":buy})
            continue

        if not {"U","u"}.issubset(d):continue
        U=int(d["U"]);u=int(d["u"])
        if u<=last:continue
        if not bridged:
            if not (U<=last+1<=u):
                continue
            bridged=True
        elif U>last+1:
            gaps+=1
            return {"status":"DEPTH_GAP","canonicalReplayReady":False,"gaps":gaps,"lastUpdateId":last,"U":U,"u":u}
        ba,bc=apply_side(bids,d.get("b",[]));aa,ac=apply_side(asks,d.get("a",[]))
        flow.append((ts,ba,bc,aa,ac))
        while flow and ts-flow[0][0]>10_000:flow.popleft()
        last=u;events+=1
        bm=book_metrics(bids,asks,5)
        if not bm:return {"status":"CROSSED_OR_EMPTY_BOOK","canonicalReplayReady":False}
        if u in ticker_by_u:
            ticker_comp+=1;tb,ta=ticker_by_u[u]
            if abs(tb-bm["best_bid"])<1e-12 and abs(ta-bm["best_ask"])<1e-12:ticker_exact+=1

        recent=[t for t in trades if ts-t["ts"]<=180_000]
        buy60=sum(t["quote"] for t in recent if ts-t["ts"]<=60_000 and t["buy"])
        sell60=sum(t["quote"] for t in recent if ts-t["ts"]<=60_000 and not t["buy"])
        ratio60=buy60/(buy60+sell60) if buy60+sell60>0 else None
        cvd3=sum(t["quote"] if t["buy"] else -t["quote"] for t in recent)
        adds=sum(x[1]+x[3] for x in flow);cancels=sum(x[2]+x[4] for x in flow)
        cancel_rate=cancels/(adds+cancels) if adds+cancels>0 else None
        ctx=historical_context(trades,ts)
        row={"ts":ts,**bm,"taker_ratio_60s":ratio60,"cvd_3m_quote":cvd3,"cancellation_rate_10s":cancel_rate,**ctx}
        samples.append(row)

        if not (eval_start_ms<=ts<eval_end_ms and ctx.get("ready")):continue
        qualifies=bool(
            ctx.get("rvol_1m") is not None and ctx["rvol_1m"]>=1.8
            and ctx.get("trade_accel") is not None and ctx["trade_accel"]>=1.5
            and ctx.get("taker_rising")
            and ctx.get("taker_latest") is not None and ctx["taker_latest"]>=0.57
            and cvd3>0
            and bm["obi"]>=0.58
            and bm["spread_bps"]<=15
            and bm["microprice_bias_bps"]>=-0.5
            and ctx.get("vwap_distance_atr") is not None and 0<=ctx["vwap_distance_atr"]<=1.5
            and ctx.get("breakout_distance_atr") is not None and -0.15<=ctx["breakout_distance_atr"]<=0.25
        )
        if qualifies and (not candidates or ts-candidates[-1]["ts"]>=60_000):
            fill=simulate_buy(asks,quote_usdt)
            candidates.append({"ts":ts,"mid":bm["mid"],"fill":fill,"context":ctx,"book":bm,"cancel_rate":cancel_rate})

    # Forward outcomes use future samples only; candidate construction above never reads them.
    for c in candidates:
        for h in (10,30,60):
            future=next((x for x in samples if x["ts"]>=c["ts"]+h*1000),None)
            if future:
                raw=(future["mid"]/c["fill"]["avg_price"]-1)*10000 if c["fill"]["avg_price"] else None
                c[f"net_{h}s_bps"]=(raw-ROUNDTRIP_COST_BPS) if raw is not None else None

    match=ticker_exact/ticker_comp if ticker_comp else None
    fillable=[c for c in candidates if c["fill"]["fill_ratio"]>=0.999]
    slips=[c["fill"]["slippage_bps"] for c in fillable if c["fill"]["slippage_bps"] is not None]
    outcomes={}
    for h in (10,30,60):
        vals=[c.get(f"net_{h}s_bps") for c in candidates if c.get(f"net_{h}s_bps") is not None]
        outcomes[str(h)]={"n":len(vals),"meanNetBps":sum(vals)/len(vals) if vals else None,"hitRate":sum(v>0 for v in vals)/len(vals) if vals else None}
    integrity=bridged and gaps==0 and events>0
    execution_ok=bool(candidates and len(fillable)==len(candidates) and (max(slips) if slips else 999)<=12)
    return {
        "status":"PASS" if integrity else "FAIL",
        "canonicalReplayReady":integrity,
        "symbol":symbol,"eventsApplied":events,"gaps":gaps,
        "tickerComparable":ticker_comp,"tickerExact":ticker_exact,"tickerMatchRate":match,
        "candidateCount":len(candidates),"fillableCount":len(fillable),
        "slippageBps":{"median":median(slips),"max":max(slips) if slips else None},
        "outcomes":outcomes,
        "executionReplayPass":execution_ok,
        "edgeProven":False,
        "liveReady":False,
        "candidates":candidates[:50],
        "note":"Validates Spot L2/aggTrade execution and microstructure only; not a full V3 strategy proof.",
    }


def run(symbol,date,start_offset,minutes,warmup_from_zero=True,quote_usdt=10.0):
    body=fetch_window(symbol,date,start_offset,minutes,warmup_from_zero)
    rows=parse_ordered_raw(body)
    day0=datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
    start=int(day0.timestamp()*1000)+start_offset*60_000
    end=start+minutes*60_000
    out=replay(rows,symbol,start,end,quote_usdt)
    return {
        "engine":"TARDIS_BINANCE_SPOT_V3_MICROSTRUCTURE_REPLAY",
        "authorization":AUTHORIZATION,"liveTrading":False,
        "date":date,"startOffset":start_offset,"minutes":minutes,
        "warmupFromZero":warmup_from_zero,"quoteUSDT":quote_usdt,
        **out,
    }


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--symbol",required=True)
    ap.add_argument("--date",default="2021-09-01")
    ap.add_argument("--start-offset",type=int,default=25)
    ap.add_argument("--minutes",type=int,default=5)
    ap.add_argument("--quote-usdt",type=float,default=10.0)
    ap.add_argument("--output",type=pathlib.Path)
    ap.add_argument("--assert-replay",action="store_true")
    a=ap.parse_args()
    out=run(a.symbol,a.date,a.start_offset,a.minutes,True,a.quote_usdt)
    text=json.dumps(out,indent=2,sort_keys=True)
    if a.output:
        a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(text)
    print(text)
    if a.assert_replay and not out.get("canonicalReplayReady"):raise SystemExit(2)


if __name__=="__main__":main()
