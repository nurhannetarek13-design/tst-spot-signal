"""Early-momentum microstructure engine for Binance Spot.

Research/PAPER signal layer only. Uses closed 1m/3m candles plus live Spot
depth/aggTrades snapshots to identify acceleration before a large breakout.
"""

from __future__ import annotations

import math
import time
import urllib.parse

WEIGHTS = {
    "volume_acceleration": 20,
    "taker_buy_acceleration": 20,
    "cvd_delta": 15,
    "order_book_imbalance": 15,
    "trade_count_acceleration": 10,
    "vwap_position": 5,
    "ema_slope": 5,
    "volatility_expansion": 5,
    "breakout_proximity": 5,
}


def median(values):
    xs=sorted(float(x) for x in values if x is not None and math.isfinite(float(x)))
    if not xs:
        return None
    m=len(xs)//2
    return xs[m] if len(xs)%2 else (xs[m-1]+xs[m])/2


def ema_series(values,n):
    if len(values)<n:
        return []
    out=[None]*(n-1)
    value=sum(values[:n])/n
    out.append(value)
    a=2/(n+1)
    for x in values[n:]:
        value=x*a+value*(1-a)
        out.append(value)
    return out


def atr(bars,n=14):
    if len(bars)<n+1:
        return None
    vals=[]
    for i in range(len(bars)-n,len(bars)):
        prev=bars[i-1]["c"]
        vals.append(max(bars[i]["h"]-bars[i]["l"],abs(bars[i]["h"]-prev),abs(bars[i]["l"]-prev)))
    return sum(vals)/len(vals)


def session_vwap(bars):
    if not bars:
        return None
    day=None
    pv=0.0
    vol=0.0
    for b in bars:
        d=time.strftime("%Y-%m-%d",time.gmtime(b["t"]/1000))
        if d!=day:
            day=d
            pv=0.0
            vol=0.0
        typical=(b["h"]+b["l"]+b["c"])/3.0
        pv+=typical*b["v"]
        vol+=b["v"]
    return pv/vol if vol>0 else None


def ratio_to_prior_median(bars,field,n=20):
    if len(bars)<n+1:
        return None
    base=median([x.get(field) for x in bars[-n-1:-1]])
    latest=float(bars[-1].get(field) or 0)
    return latest/base if base and base>0 else None


def taker_ratio(bar):
    q=float(bar.get("qv") or 0)
    return float(bar.get("tq") or 0)/q if q>0 else None


def candle_cvd_series(bars):
    """Quote-volume delta: aggressive buy quote - aggressive sell quote."""
    return [2.0*float(b.get("tq") or 0)-float(b.get("qv") or 0) for b in bars]


def positive_slope(values,lookback=3):
    if len(values)<lookback:
        return False
    x=values[-lookback:]
    return x[-1]>x[0] and sum(x)>0


def breakout_proximity(bars,atr_abs,lookback=20):
    if len(bars)<lookback+1 or not atr_abs or atr_abs<=0:
        return None
    resistance=max(x["h"] for x in bars[-lookback-1:-1])
    distance=(resistance-bars[-1]["c"])/atr_abs
    return {"resistance":resistance,"distance_atr":distance}


def prefilter_snapshot(bars1m,bars3m,cfg):
    if len(bars1m)<40 or len(bars3m)<25:
        raise ValueError("INSUFFICIENT_MICRO_HISTORY")

    rvol1=ratio_to_prior_median(bars1m,"qv",20)
    rvol3=ratio_to_prior_median(bars3m,"qv",20)
    trade_accel=ratio_to_prior_median(bars1m,"n",20)
    takers=[taker_ratio(x) for x in bars1m[-3:]]
    taker_rising=bool(all(x is not None for x in takers) and takers[0]<takers[1]<takers[2])
    latest_taker=takers[-1] if takers else None

    cvd=candle_cvd_series(bars1m)
    cvd_positive=positive_slope(cvd,3)

    closes=[x["c"] for x in bars1m]
    ema9=ema_series(closes,9)
    ema_slope=bool(len(ema9)>=4 and ema9[-1] is not None and ema9[-4] is not None and ema9[-1]>ema9[-4])

    atr_now=atr(bars1m,14)
    older_atrs=[]
    if len(bars1m)>=35:
        for end in range(len(bars1m)-6,len(bars1m)):
            subset=bars1m[:end]
            a=atr(subset,14)
            if a: older_atrs.append(a)
    atr_base=median(older_atrs)
    vol_expansion=bool(atr_now and atr_base and atr_now/atr_base>=float(cfg["volatility_expansion_ratio"]))

    vw=session_vwap(bars1m)
    price=bars1m[-1]["c"]
    vwap_distance_atr=((price-vw)/atr_now) if vw is not None and atr_now and atr_now>0 else None
    vwap_ok=bool(vwap_distance_atr is not None and 0.0<=vwap_distance_atr<=float(cfg["vwap_max_atr_distance"]))

    prox=breakout_proximity(bars1m,atr_now,int(cfg["recent_resistance_lookback_1m"]))
    near_breakout=bool(prox and -0.15<=prox["distance_atr"]<=float(cfg["breakout_proximity_atr"]))

    velocity_lb=int(cfg["price_velocity_lookback_1m"])
    velocity=(price/bars1m[-1-velocity_lb]["c"]-1.0) if len(bars1m)>velocity_lb else 0.0

    points=0
    if rvol1 is not None and rvol1>=float(cfg["volume_acceleration_1m_watch"]):
        points+=12
        if rvol1>=float(cfg["volume_acceleration_1m_armed"]) and rvol3 is not None and rvol3>=float(cfg["volume_acceleration_3m_armed"]):
            points+=8
    if latest_taker is not None and latest_taker>=float(cfg["taker_watch_min"]):
        points+=10
        if taker_rising and latest_taker>=float(cfg["taker_entry_min"]):
            points+=10
    if cvd_positive:
        points+=15
    if trade_accel is not None and trade_accel>=float(cfg["trade_count_watch"]):
        points+=6
        if trade_accel>=float(cfg["trade_count_armed"]):
            points+=4
    if vwap_ok:
        points+=5
    if ema_slope:
        points+=5
    if vol_expansion:
        points+=5
    if near_breakout:
        points+=5

    return {
        "prefilter_score":points,
        "rvol_1m":rvol1,
        "rvol_3m":rvol3,
        "trade_count_accel":trade_accel,
        "taker_last3":takers,
        "taker_rising":taker_rising,
        "taker_latest":latest_taker,
        "cvd_positive":cvd_positive,
        "ema9_slope_positive":ema_slope,
        "volatility_expansion":vol_expansion,
        "vwap":vw,
        "vwap_distance_atr":vwap_distance_atr,
        "vwap_position_ok":vwap_ok,
        "atr_1m":atr_now,
        "breakout":prox,
        "breakout_proximity_ok":near_breakout,
        "price_velocity":velocity,
    }



def micro_breakout_hold(bars,lookback=20,tolerance_pct=0.0015):
    """Require a small breakout/reclaim without waiting for a large extension."""
    if len(bars)<lookback+3:
        return {"ok":False,"resistance":None,"breakout":False,"hold":False}
    resistance=max(x["h"] for x in bars[-lookback-3:-3])
    recent=bars[-3:]
    breakout=any(x["h"]>resistance for x in recent)
    hold=bool(recent[-1]["c"]>=resistance*(1.0-float(tolerance_pct)))
    return {"ok":bool(breakout and hold),"resistance":resistance,"breakout":breakout,"hold":hold}


def depth_imbalance(depth,levels=5):
    bids=depth.get("bids",[])[:levels]
    asks=depth.get("asks",[])[:levels]
    bid_liq=sum(float(px)*float(qty) for px,qty in bids)
    ask_liq=sum(float(px)*float(qty) for px,qty in asks)
    den=bid_liq+ask_liq
    return bid_liq/den if den>0 else None


def aggtrade_delta(aggtrades,window_ms=180000):
    if not aggtrades:
        return {"delta_quote":0.0,"buy_quote":0.0,"sell_quote":0.0,"ratio":None,"slope_positive":False}
    latest=max(int(x.get("T") or 0) for x in aggtrades)
    rows=[x for x in aggtrades if latest-int(x.get("T") or 0)<=window_ms]
    buckets={}
    buy=sell=0.0
    for x in rows:
        quote=float(x["p"])*float(x["q"])
        ts=int(x.get("T") or 0)
        minute=ts//60000
        # m=true => buyer is maker => sell aggressor.
        if bool(x.get("m")):
            sell+=quote
            buckets[minute]=buckets.get(minute,0.0)-quote
        else:
            buy+=quote
            buckets[minute]=buckets.get(minute,0.0)+quote
    vals=[buckets[k] for k in sorted(buckets)]
    ratio=buy/(buy+sell) if buy+sell>0 else None
    return {
        "delta_quote":buy-sell,
        "buy_quote":buy,
        "sell_quote":sell,
        "ratio":ratio,
        "slope_positive":positive_slope(vals,min(3,len(vals))) if vals else False,
    }


def classify(score,cfg):
    if score>=float(cfg["entry_candidate_min"]): return "ENTRY_CANDIDATE"
    if score>=float(cfg["armed_min"]): return "ARMED"
    if score>=float(cfg["watch_min"]): return "WATCH"
    return "IGNORE"


def combine_microstructure(pre,obi_samples,spread_samples,agg,cfg):
    ibis=[x for x in obi_samples if x is not None]
    obi=min(ibis) if ibis else None  # persistence: weakest sample must still hold.
    spread_tight=bool(
        len(spread_samples)>=2
        and spread_samples[-1] <= spread_samples[0]*float(cfg["spread_stable_multiplier"])
    )
    obi_ok=bool(obi is not None and obi>=float(cfg["obi_watch_min"]))
    obi_armed=bool(obi is not None and obi>=float(cfg["obi_armed_min"]))

    score=float(pre["prefilter_score"])
    if obi_ok:
        score+=8
        if obi_armed: score+=7

    # CVD has a fixed 15-point budget in the pre-score. Live aggTrades are
    # confirmation for ENTRY_CANDIDATE, not extra points (avoids double counting).
    score=min(100.0,score)

    chase=False
    distance=pre.get("vwap_distance_atr")
    if distance is not None:
        chase=distance>float(cfg["vwap_max_atr_distance"])

    stage=classify(score,cfg)
    micro_hold=pre.get("micro_breakout_hold") or {"ok":False}
    if stage=="ENTRY_CANDIDATE":
        required=(
            pre.get("rvol_1m") is not None and pre["rvol_1m"]>=float(cfg["volume_acceleration_1m_armed"])
            and pre.get("taker_latest") is not None and pre["taker_latest"]>=float(cfg["taker_entry_min"])
            and pre.get("taker_rising")
            and agg.get("delta_quote",0)>0
            and obi_armed
            and spread_tight
            and pre.get("vwap_position_ok")
            and micro_hold.get("ok")
            and not chase
        )
        if not required:
            stage="ARMED"

    return {
        **pre,
        "score":score,
        "stage":stage,
        "obi":obi,
        "obi_samples":obi_samples,
        "spread_samples_bps":spread_samples,
        "spread_stable_or_tightening":spread_tight,
        "agg_cvd":agg,
        "micro_breakout_hold":micro_hold,
        "chase_veto":chase,
    }
