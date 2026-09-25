"""Smart execution planning for PAPER/SHADOW Spot entries.

This module chooses an execution style and estimates fill quality. It does not
place live orders. Live order placement stays behind the existing execution
state machine and explicit live gate.
"""

from __future__ import annotations

import math


def _finite(value):
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _round_up(value, step):
    if not (_finite(value) and _finite(step)) or float(step)<=0:
        return float(value)
    v=float(value); s=float(step)
    return math.ceil(v/s-1e-12)*s


def _round_down(value, step):
    if not (_finite(value) and _finite(step)) or float(step)<=0:
        return float(value)
    v=float(value); s=float(step)
    return math.floor(v/s+1e-12)*s


def choose_execution_plan(*, symbol, quote_amount_usdt, best_bid, best_ask,
                          estimated_slippage_bps, fill_ratio, tick_size,
                          momentum_score, taker_rising, spread_bps,
                          latency_ms, cfg):
    """Choose MARKET vs aggressive LIMIT in shadow/paper.

    Fast momentum can justify MARKET only when book depth/spread are excellent.
    Otherwise use a capped aggressive LIMIT. If fill quality is poor, reject.
    """
    c=cfg
    reasons=[]
    quote=float(quote_amount_usdt)
    bid=float(best_bid)
    ask=float(best_ask)
    slip=float(estimated_slippage_bps) if estimated_slippage_bps is not None else float("inf")
    fill=float(fill_ratio or 0.0)
    spread=float(spread_bps)
    latency=float(latency_ms)

    if not (quote>0 and bid>0 and ask>=bid):
        return {"ok":False,"reason":"EXECUTION_BOOK_INVALID"}
    if fill<float(c["min_fill_ratio"]):
        return {"ok":False,"reason":"EXECUTION_DEPTH_TOO_THIN","fill_ratio":fill}
    if slip>float(c["max_slippage_bps"]):
        return {"ok":False,"reason":"EXECUTION_SLIPPAGE_TOO_HIGH","slippage_bps":slip}
    if latency>float(c["max_latency_ms"]):
        return {"ok":False,"reason":"EXECUTION_LATENCY_TOO_HIGH","latency_ms":latency}

    urgent=bool(
        float(momentum_score)>=float(c["market_momentum_score"])
        and bool(taker_rising)
    )
    market_ok=bool(
        urgent
        and spread<=float(c["market_max_spread_bps"])
        and slip<=float(c["market_max_slippage_bps"])
    )

    if market_ok:
        return {
            "ok":True,
            "style":"MARKET",
            "symbol":symbol,
            "quote_amount_usdt":quote,
            "reference_price":ask,
            "estimated_slippage_bps":slip,
            "fill_ratio":fill,
            "cancel_after_ms":None,
            "max_cancel_replace":0,
            "reason":"URGENT_TIGHT_BOOK",
        }

    # Aggressive limit crosses near the ask but caps chasing.
    max_cross_bps=float(c["aggressive_limit_max_cross_bps"])
    cap=ask*(1+max_cross_bps/10000.0)
    price=_round_up(min(cap,ask*(1+max(0.0,slip)/10000.0)),tick_size)
    if price<=0:
        return {"ok":False,"reason":"INVALID_LIMIT_PRICE"}

    return {
        "ok":True,
        "style":"AGGRESSIVE_LIMIT",
        "symbol":symbol,
        "quote_amount_usdt":quote,
        "limit_price":price,
        "reference_price":ask,
        "estimated_slippage_bps":slip,
        "fill_ratio":fill,
        "cancel_after_ms":int(c["cancel_after_ms"]),
        "max_cancel_replace":int(c["max_cancel_replace"]),
        "reason":"CONTROLLED_PRICE_CAP",
    }


def realized_slippage_bps(reference_price, average_fill_price, side="BUY"):
    ref=float(reference_price)
    fill=float(average_fill_price)
    if ref<=0 or fill<=0:
        return None
    if str(side).upper()=="BUY":
        return (fill/ref-1.0)*10000.0
    return (ref/fill-1.0)*10000.0


def update_symbol_slippage_model(model, symbol, realized_bps, alpha=0.20):
    """EWMA + max tracking for per-coin execution diagnostics."""
    if realized_bps is None or not _finite(realized_bps):
        return model
    out=dict(model or {})
    row=dict(out.get(symbol) or {})
    x=max(0.0,float(realized_bps))
    count=int(row.get("count") or 0)
    ewma=x if count==0 else (float(alpha)*x+(1-float(alpha))*float(row.get("ewma_bps") or 0.0))
    row.update({
        "count":count+1,
        "ewma_bps":ewma,
        "max_bps":max(float(row.get("max_bps") or 0.0),x),
        "last_bps":x,
    })
    out[symbol]=row
    return out
