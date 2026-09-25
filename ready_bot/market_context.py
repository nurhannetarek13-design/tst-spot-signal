"""Market context helpers for the active Binance Spot PAPER engine.

These are not extra entry indicators. They provide:
- bar/data-integrity checks
- broad market regime classification
- cross-sectional relative-strength ranking
- portfolio correlation estimates
"""

from __future__ import annotations

import math
import statistics


INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}


def _finite(x):
    try:
        return math.isfinite(float(x))
    except Exception:
        return False


def validate_bar_integrity(bars, interval, now_ms, *, recent_window=30, stale_multiple=3.0):
    step=INTERVAL_MS.get(str(interval))
    reasons=[]
    if step is None:
        return {"ok":False,"reasons":["UNKNOWN_INTERVAL"]}
    if not bars:
        return {"ok":False,"reasons":["NO_BARS"]}

    times=[int(x["t"]) for x in bars if "t" in x]
    if len(times)!=len(bars):
        reasons.append("MISSING_TIMESTAMPS")
    if any(times[i]<=times[i-1] for i in range(1,len(times))):
        reasons.append("NON_MONOTONIC_TIMESTAMPS")

    recent=times[-min(len(times),int(recent_window)):]
    gaps=[recent[i]-recent[i-1] for i in range(1,len(recent))]
    max_gap=max(gaps) if gaps else step
    # One missing candle may occur in sparse markets; >=2 missing intervals is fail-closed.
    if max_gap>step*2.1:
        reasons.append("BAR_GAP")

    last_close=times[-1]+step
    age=max(0,int(now_ms)-last_close)
    if age>step*float(stale_multiple):
        reasons.append("STALE_BARS")
    if last_close>int(now_ms)+step:
        reasons.append("FUTURE_BAR")

    return {
        "ok":not reasons,
        "reasons":reasons,
        "last_close_age_ms":age,
        "max_gap_ms":max_gap,
        "interval_ms":step,
    }


def pct_return(bars, lookback_bars):
    n=int(lookback_bars)
    if not bars or len(bars)<=n:
        return None
    a=float(bars[-1-n]["c"])
    b=float(bars[-1]["c"])
    if a<=0 or not (_finite(a) and _finite(b)):
        return None
    return b/a-1.0


def ema(values,n):
    if len(values)<n:
        return None
    a=2/(n+1)
    value=sum(values[:n])/n
    for x in values[n:]:
        value=x*a+value*(1-a)
    return value


def _tr_values(bars):
    out=[]
    for i in range(1,len(bars)):
        prev=float(bars[i-1]["c"])
        h=float(bars[i]["h"])
        l=float(bars[i]["l"])
        out.append(max(h-l,abs(h-prev),abs(l-prev)))
    return out


def atr_percent(bars,n=14):
    if len(bars)<n+1:
        return None
    trs=_tr_values(bars)
    close=float(bars[-1]["c"])
    if close<=0:
        return None
    return (sum(trs[-n:])/n)/close


def atr_regime_ratio(bars,n=14,history=48):
    if len(bars)<n+history+2:
        return None
    vals=[]
    for end in range(len(bars)-history,len(bars)+1):
        sub=bars[:end]
        x=atr_percent(sub,n)
        if x is not None and _finite(x):
            vals.append(x)
    if len(vals)<10:
        return None
    current=vals[-1]
    base=statistics.median(vals[:-1])
    return current/base if base>0 else None


def classify_market_regime(btc1h,btc4h,snapshots):
    c1=[float(x["c"]) for x in btc1h]
    c4=[float(x["c"]) for x in btc4h]
    ret1=pct_return(btc1h,1)
    ret4=pct_return(btc1h,4)
    e20_1=ema(c1,20)
    e50_1=ema(c1,50)
    e50_4=ema(c4,50)
    e200_4=ema(c4,200)
    atr_ratio=atr_regime_ratio(btc1h,14,48)

    rows=[
        s for s in snapshots.values()
        if s.get("ret_1h") is not None and s.get("ret_15m") is not None
    ]
    breadth_1h=(sum(float(s["ret_1h"])>0 for s in rows)/len(rows)) if rows else 0.0
    breadth_15m=(sum(float(s["ret_15m"])>0 for s in rows)/len(rows)) if rows else 0.0

    macro_bull=bool(e50_4 is not None and e200_4 is not None and e50_4>e200_4)
    trend_1h=bool(e20_1 is not None and e50_1 is not None and e20_1>e50_1)

    risk_off=bool(
        ret1 is None
        or ret1<=-0.015
        or (not macro_bull and breadth_1h<0.35)
        or breadth_15m<0.20
    )
    high_vol=bool(
        not risk_off
        and (
            (atr_ratio is not None and atr_ratio>=1.8)
            or (ret1 is not None and abs(ret1)>=0.012)
        )
    )
    trend=bool(
        not risk_off
        and not high_vol
        and macro_bull
        and trend_1h
        and breadth_1h>=0.55
    )

    if risk_off:
        state="RISK_OFF"
        risk_multiplier=0.0
    elif high_vol:
        state="HIGH_VOLATILITY"
        risk_multiplier=0.50
    elif trend:
        state="TREND"
        risk_multiplier=1.0
    else:
        state="RANGE"
        risk_multiplier=0.75

    return {
        "state":state,
        "allow_new_longs":state!="RISK_OFF",
        "risk_multiplier":risk_multiplier,
        "btc_ret_1h":ret1,
        "btc_ret_4h":ret4,
        "btc_atr_regime_ratio":atr_ratio,
        "macro_bull":macro_bull,
        "trend_1h":trend_1h,
        "breadth_1h":breadth_1h,
        "breadth_15m":breadth_15m,
        "symbols_in_breadth":len(rows),
    }


def _percentile_ranks(values_by_symbol):
    clean={s:float(v) for s,v in values_by_symbol.items() if v is not None and _finite(v)}
    if not clean:
        return {}
    ordered=sorted(clean.items(),key=lambda kv:(kv[1],kv[0]))
    n=len(ordered)
    if n==1:
        return {ordered[0][0]:1.0}
    return {s:i/(n-1) for i,(s,_) in enumerate(ordered)}


def relative_strength_ranking(snapshots,btc_returns=None):
    btc_returns=btc_returns or {}
    r5={s:x.get("ret_5m") for s,x in snapshots.items()}
    r15={s:x.get("ret_15m") for s,x in snapshots.items()}
    r1={s:x.get("ret_1h") for s,x in snapshots.items()}
    p5=_percentile_ranks(r5)
    p15=_percentile_ranks(r15)
    p1=_percentile_ranks(r1)

    out={}
    for symbol,snap in snapshots.items():
        if symbol not in p5 or symbol not in p15 or symbol not in p1:
            continue
        score=100.0*(0.40*p5[symbol]+0.35*p15[symbol]+0.25*p1[symbol])
        excess={
            "5m":float(snap["ret_5m"])-float(btc_returns.get("5m") or 0.0),
            "15m":float(snap["ret_15m"])-float(btc_returns.get("15m") or 0.0),
            "1h":float(snap["ret_1h"])-float(btc_returns.get("1h") or 0.0),
        }
        out[symbol]={
            "score":score,
            "percentile_5m":p5[symbol],
            "percentile_15m":p15[symbol],
            "percentile_1h":p1[symbol],
            "excess_vs_btc":excess,
            "strong":bool(score>=65 and sum(v>0 for v in excess.values())>=2),
            "weak":bool(score<30 and sum(v<0 for v in excess.values())>=2),
        }
    return out


def return_series(bars,points=48):
    if len(bars)<points+1:
        return []
    closes=[float(x["c"]) for x in bars[-points-1:]]
    return [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes)) if closes[i-1]>0]


def pearson_corr(a,b):
    n=min(len(a),len(b))
    if n<12:
        return None
    x=a[-n:]
    y=b[-n:]
    mx=sum(x)/n
    my=sum(y)/n
    vx=sum((v-mx)**2 for v in x)
    vy=sum((v-my)**2 for v in y)
    if vx<=0 or vy<=0:
        return None
    cov=sum((x[i]-mx)*(y[i]-my) for i in range(n))
    return cov/math.sqrt(vx*vy)


def max_open_position_correlation(candidate_snap,open_symbols,snapshots,points=48):
    candidate=return_series(candidate_snap.get("_bars1h") or [],points)
    best=None
    best_symbol=None
    for symbol in open_symbols:
        other=snapshots.get(symbol)
        if not other:
            continue
        corr=pearson_corr(candidate,return_series(other.get("_bars1h") or [],points))
        if corr is not None and (best is None or corr>best):
            best=corr
            best_symbol=symbol
    return {"max_corr":best,"symbol":best_symbol}
