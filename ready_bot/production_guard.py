"""Production-safety guardrails for the Spot early-momentum runtime.

These helpers are deliberately orthogonal to entry indicators. They answer:
- are clocks/data fresh enough to act?
- did BTC just shock the market?
- is bullish book pressure confirmed by actual aggressive trades?
- is liquidity disappearing?
- can the requested quote size be filled without unacceptable slippage?
- is the runtime warmed up and outside explicit event-risk windows?
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path


def _finite(value):
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def clock_sync_status(server_time_ms, request_start_ms, response_end_ms, *,
                      max_offset_ms=750, max_rtt_ms=1500):
    start=float(request_start_ms)
    end=float(response_end_ms)
    server=float(server_time_ms)
    rtt=max(0.0,end-start)
    midpoint=start+rtt/2.0
    offset=server-midpoint
    ok=abs(offset)<=float(max_offset_ms) and rtt<=float(max_rtt_ms)
    reasons=[]
    if abs(offset)>float(max_offset_ms): reasons.append("CLOCK_OFFSET")
    if rtt>float(max_rtt_ms): reasons.append("CLOCK_RTT")
    return {
        "ok":ok,
        "offset_ms":offset,
        "rtt_ms":rtt,
        "reasons":reasons,
    }


def signal_freshness_status(*, detected_at_ms, decision_at_ms=None, now_ms=None,
                            market_event_ms=None,
                            max_signal_age_ms=12000, max_decision_latency_ms=6500):
    now=float(now_ms if now_ms is not None else time.time()*1000)
    detected=float(detected_at_ms or 0)
    decision=float(decision_at_ms or now)
    event=float(market_event_ms or detected or 0)
    age=now-event if event>0 else float("inf")
    decision_latency=decision-detected if detected>0 else float("inf")
    reasons=[]
    if age<0 or age>float(max_signal_age_ms): reasons.append("STALE_SIGNAL")
    if decision_latency<0 or decision_latency>float(max_decision_latency_ms): reasons.append("LATENCY_BUDGET")
    return {
        "ok":not reasons,
        "age_ms":age,
        "decision_latency_ms":decision_latency,
        "reasons":reasons,
    }


def warmup_status(*, bars1m=0, bars3m=0, bars15m=0, bars1h=0, depth_samples=0,
                  min_1m=60, min_3m=30, min_15m=220, min_1h=220, min_depth_samples=3):
    counts={
        "1m":int(bars1m),"3m":int(bars3m),"15m":int(bars15m),
        "1h":int(bars1h),"depth":int(depth_samples),
    }
    minimums={
        "1m":int(min_1m),"3m":int(min_3m),"15m":int(min_15m),
        "1h":int(min_1h),"depth":int(min_depth_samples),
    }
    missing=[name for name in counts if counts[name]<minimums[name]]
    return {"ok":not missing,"missing":missing,"counts":counts,"minimums":minimums}


def btc_shock_status(bars1m, *, shock_1m=0.008, shock_3m=0.015):
    if not bars1m or len(bars1m)<4:
        return {"ok":False,"shock":True,"reason":"BTC_SHOCK_WARMUP","ret_1m":None,"ret_3m":None}
    close=float(bars1m[-1]["c"])
    p1=float(bars1m[-2]["c"])
    p3=float(bars1m[-4]["c"])
    r1=close/p1-1.0 if p1>0 else 0.0
    r3=close/p3-1.0 if p3>0 else 0.0
    shock=abs(r1)>=float(shock_1m) or abs(r3)>=float(shock_3m)
    return {
        "ok":not shock,
        "shock":shock,
        "reason":"BTC_SHOCK" if shock else None,
        "ret_1m":r1,
        "ret_3m":r3,
    }


def adverse_selection_status(micro, *, obi_bullish=0.58, min_trade_ratio=0.52,
                             min_microprice_bias_bps=-0.5,
                             max_bid_liquidity_drop_pct=0.35):
    """Reject bullish-looking book states contradicted by executed flow.

    Book imbalance is not trusted by itself. A bullish OBI must not coexist with:
    negative live delta, weak aggressive-buy ratio, negative microprice bias, or
    rapid disappearance of bid liquidity.
    """
    micro=micro or {}
    obi=micro.get("obi")
    agg=micro.get("agg_cvd") or {}
    flow=micro.get("depth_flow") or {}
    reasons=[]

    bullish_book=obi is not None and float(obi)>=float(obi_bullish)
    ratio=agg.get("ratio")
    delta=float(agg.get("delta_quote") or 0.0)
    bias=flow.get("microprice_bias_bps")
    bid_change=flow.get("bid_liquidity_change_pct")

    if bullish_book:
        if delta<=0: reasons.append("OBI_CVD_DIVERGENCE")
        if ratio is None or float(ratio)<float(min_trade_ratio): reasons.append("OBI_TRADE_FLOW_DIVERGENCE")
        if bias is not None and float(bias)<float(min_microprice_bias_bps): reasons.append("NEGATIVE_MICROPRICE")
        if bid_change is not None and float(bid_change)<=-abs(float(max_bid_liquidity_drop_pct)):
            reasons.append("BID_LIQUIDITY_DISAPPEARANCE")

    return {
        "ok":not reasons,
        "reasons":reasons,
        "bullish_book":bullish_book,
        "obi":obi,
        "trade_ratio":ratio,
        "delta_quote":delta,
        "microprice_bias_bps":bias,
        "bid_liquidity_change_pct":bid_change,
    }


def liquidity_disappearance_status(depth_flow, *, max_bid_drop_pct=0.35,
                                   max_ask_growth_pct=0.50,
                                   max_cancellation_rate=0.75,
                                   bid_cancel_imbalance_ratio=1.50):
    flow=depth_flow or {}
    bid=flow.get("bid_liquidity_change_pct")
    ask=flow.get("ask_liquidity_change_pct")
    cancellation=flow.get("cancellation_rate_10s")
    bid_cancel=flow.get("bid_cancel_quote_10s")
    ask_cancel=flow.get("ask_cancel_quote_10s")
    reasons=[]
    if bid is not None and float(bid)<=-abs(float(max_bid_drop_pct)):
        reasons.append("BID_LIQUIDITY_DISAPPEARANCE")
    if ask is not None and float(ask)>=abs(float(max_ask_growth_pct)):
        reasons.append("ASK_LIQUIDITY_SURGE")
    if cancellation is not None and float(cancellation)>=float(max_cancellation_rate):
        if bid_cancel is not None and ask_cancel is not None:
            if float(bid_cancel)>max(float(ask_cancel),1e-9)*float(bid_cancel_imbalance_ratio):
                reasons.append("BID_CANCELLATION_SPIKE")
    return {
        "ok":not reasons,"reasons":reasons,
        "bid_change_pct":bid,"ask_change_pct":ask,
        "cancellation_rate_10s":cancellation,
        "bid_cancel_quote_10s":bid_cancel,
        "ask_cancel_quote_10s":ask_cancel,
    }


def estimate_buy_slippage(depth, quote_amount_usdt):
    """Walk ask levels for a quote-sized BUY and return deterministic fill metrics."""
    quote=float(quote_amount_usdt)
    asks=(depth or {}).get("asks") or []
    if quote<=0 or not asks:
        return {"ok":False,"reason":"DEPTH_UNAVAILABLE","fill_ratio":0.0,"slippage_bps":None}

    remaining=quote
    base_qty=0.0
    spent=0.0
    best=float(asks[0][0])
    for level in asks:
        px=float(level[0])
        qty=float(level[1])
        available_quote=max(0.0,px*qty)
        take=min(remaining,available_quote)
        if take>0 and px>0:
            base_qty+=take/px
            spent+=take
            remaining-=take
        if remaining<=1e-12:
            break

    fill_ratio=min(1.0,spent/quote) if quote>0 else 0.0
    avg=spent/base_qty if base_qty>0 else None
    slip=((avg/best)-1.0)*10000 if avg is not None and best>0 else None
    return {
        "ok":fill_ratio>=0.999 and slip is not None,
        "fill_ratio":fill_ratio,
        "average_price":avg,
        "best_ask":best,
        "slippage_bps":slip,
        "quote_simulated":spent,
    }


def estimate_sell_slippage(depth, base_qty):
    """Walk bid levels for a base-sized SELL and return deterministic fill metrics."""
    qty=float(base_qty)
    bids=(depth or {}).get("bids") or []
    if qty<=0 or not bids:
        return {"ok":False,"reason":"DEPTH_UNAVAILABLE","fill_ratio":0.0,"slippage_bps":None}

    remaining=qty
    sold=0.0
    quote=0.0
    best=float(bids[0][0])
    for level in bids:
        px=float(level[0])
        available=max(0.0,float(level[1]))
        take=min(remaining,available)
        if take>0 and px>0:
            sold+=take
            quote+=take*px
            remaining-=take
        if remaining<=1e-12:
            break

    fill_ratio=min(1.0,sold/qty) if qty>0 else 0.0
    avg=quote/sold if sold>0 else None
    slip=(1.0-avg/best)*10000 if avg is not None and best>0 else None
    return {
        "ok":fill_ratio>=0.999 and slip is not None,
        "fill_ratio":fill_ratio,
        "average_price":avg,
        "best_bid":best,
        "slippage_bps":slip,
        "base_simulated":sold,
        "quote_proceeds":quote,
    }


def execution_quality_status(depth, quote_amount_usdt, *, max_slippage_bps=12,
                             min_fill_ratio=0.999):
    estimate=estimate_buy_slippage(depth,quote_amount_usdt)
    reasons=[]
    if estimate["fill_ratio"]<float(min_fill_ratio): reasons.append("INSUFFICIENT_DEPTH")
    slip=estimate.get("slippage_bps")
    if slip is None or float(slip)>float(max_slippage_bps): reasons.append("SLIPPAGE_TOO_HIGH")
    return {"ok":not reasons,"reasons":reasons,**estimate}


def utc_session_label(now=None):
    t=now or datetime.now(timezone.utc)
    h=t.hour
    if 0<=h<7: return "ASIA"
    if 7<=h<13: return "EUROPE"
    if 13<=h<16: return "EU_US_OVERLAP"
    if 16<=h<21: return "US"
    return "LATE_US_ASIA_TRANSITION"


def load_event_risk(path, symbol, now=None):
    """Explicit operational event-risk file.

    This is intentionally deterministic: uncertain external news never gets
    silently converted into a trade. Operators/automation may populate the file.
    """
    p=Path(path)
    if not p.exists():
        return {"ok":True,"blocked":False,"reason":None,"source":"NO_FILE"}
    try:
        data=json.loads(p.read_text())
    except Exception as exc:
        return {"ok":False,"blocked":True,"reason":"EVENT_RISK_FILE_INVALID","detail":str(exc)[:120]}

    now_ts=(now or datetime.now(timezone.utc)).timestamp()

    def active_until(value):
        if not value:
            return False
        try:
            if isinstance(value,(int,float)): return float(value)>now_ts
            return datetime.fromisoformat(str(value).replace("Z","+00:00")).timestamp()>now_ts
        except Exception:
            return True

    if active_until(data.get("global_halt_until")):
        return {"ok":False,"blocked":True,"reason":"GLOBAL_EVENT_RISK","detail":data.get("global_reason")}

    row=(data.get("symbols") or {}).get(str(symbol).upper())
    if isinstance(row,dict) and active_until(row.get("until")):
        return {"ok":False,"blocked":True,"reason":"SYMBOL_EVENT_RISK","detail":row.get("reason")}

    return {"ok":True,"blocked":False,"reason":None,"source":"EVENT_RISK_FILE"}
