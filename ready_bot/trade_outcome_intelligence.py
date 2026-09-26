"""Post-trade outcome attribution for PAPER/SHADOW evidence only.

This module is deliberately non-authorizing: it explains closed trades and
aggregates evidence. It must never enable live execution or increase sizing.
"""
from __future__ import annotations
from collections import defaultdict
import math

SCHEMA_VERSION = 1

def _f(v, default=0.0):
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default

def attribute_closed_trade(trade: dict) -> dict:
    if not isinstance(trade, dict):
        raise ValueError("trade must be a dict")
    if not trade.get("closed_at") and trade.get("status") not in {"CLOSED","EXITED"}:
        raise ValueError("attribution requires a closed trade")

    pnl=_f(trade.get("pnl_usdt"))
    entry=_f(trade.get("entry"))
    exit_px=_f(trade.get("exit_price"))
    reasons=[]
    positives=[]

    exit_reason=str(trade.get("exit_reason") or "UNKNOWN").upper()
    if exit_reason!="UNKNOWN":
        reasons.append(exit_reason)

    planned=trade.get("planned_entry") or trade.get("signal_price")
    if planned and entry and _f(planned)>0:
        chase_bps=(entry/_f(planned)-1.0)*10000.0
        if chase_bps>=_f(trade.get("late_entry_threshold_bps"), 8.0):
            reasons.append("LATE_ENTRY")

    if trade.get("btc_shock") is True:
        reasons.append("BTC_SHOCK")
    if trade.get("failed_breakout") is True and "FAILED_BREAKOUT" not in reasons:
        reasons.append("FAILED_BREAKOUT")
    if trade.get("quality_degraded") is True:
        reasons.append("QUALITY_DEGRADED")
    if trade.get("liquidity_degraded") is True:
        reasons.append("LIQUIDITY_DEGRADED")
    if trade.get("momentum_failed") is True and "MOMENTUM_FAILURE" not in reasons:
        reasons.append("MOMENTUM_FAILURE")

    if pnl>0:
        if trade.get("strong_relative_strength") is True:
            positives.append("STRONG_RS")
        if trade.get("volume_expansion") is True:
            positives.append("VOLUME_EXPANSION")
        if trade.get("cvd_continuation") is True:
            positives.append("CVD_CONTINUATION")
        if trade.get("structure_intact") is True:
            positives.append("STRUCTURE_HELD")
        if _f(trade.get("mfe_r"))>=1.5:
            positives.append("WINNER_EXTENSION_AVAILABLE")

    return {
        "schema_version":SCHEMA_VERSION,
        "symbol":trade.get("symbol"),
        "setup_type":trade.get("setup_type") or "UNKNOWN",
        "regime":trade.get("regime") or "UNKNOWN",
        "exit_reason":exit_reason,
        "pnl_usdt":pnl,
        "mae_r":trade.get("mae_r"),
        "mfe_r":trade.get("mfe_r"),
        "outcome":"WIN" if pnl>0 else "LOSS" if pnl<0 else "FLAT",
        "loss_factors":sorted(set(reasons)) if pnl<=0 else [],
        "win_factors":sorted(set(positives)) if pnl>0 else [],
        "diagnostic_only":True,
        "may_authorize_live":False,
        "may_increase_size":False,
    }

def aggregate_attributions(rows: list[dict], min_sample: int=30) -> dict:
    groups=defaultdict(lambda: {"trades":0,"wins":0,"pnl":0.0,"loss_factors":defaultdict(int),"win_factors":defaultdict(int)})
    for row in rows or []:
        a=row if row.get("schema_version")==SCHEMA_VERSION else attribute_closed_trade(row)
        key=f'{a.get("setup_type","UNKNOWN")}|{a.get("regime","UNKNOWN")}'
        g=groups[key]; g["trades"]+=1; g["wins"]+=int(a["outcome"]=="WIN"); g["pnl"]+=_f(a.get("pnl_usdt"))
        for x in a.get("loss_factors") or []: g["loss_factors"][x]+=1
        for x in a.get("win_factors") or []: g["win_factors"][x]+=1
    out={}
    for key,g in groups.items():
        n=g["trades"]
        out[key]={
            "trades":n,
            "win_rate":g["wins"]/n if n else None,
            "pnl_usdt":g["pnl"],
            "avg_pnl_usdt":g["pnl"]/n if n else None,
            "evidence_ready":n>=int(min_sample),
            "loss_factors":dict(sorted(g["loss_factors"].items(), key=lambda x:(-x[1],x[0]))),
            "win_factors":dict(sorted(g["win_factors"].items(), key=lambda x:(-x[1],x[0]))),
        }
    return {"schema_version":SCHEMA_VERSION,"groups":out,"diagnostic_only":True,"may_authorize_live":False}
