"""Fail-closed canary health gate and rollback decision."""
from __future__ import annotations

def canary_decision(metrics:dict, baseline:dict, thresholds:dict|None=None):
    t={"max_error_rate":0.02,"max_reject_rate":0.02,"max_latency_ratio":1.5,"max_slippage_delta_bps":5.0}
    t.update(thresholds or {})
    reasons=[]
    signals=max(1,int(metrics.get("signals",0)))
    errors=int(metrics.get("api_errors",0))/signals
    rejects=int(metrics.get("rejected_orders",0))/signals
    if errors>float(t["max_error_rate"]): reasons.append("API_ERROR_RATE")
    if rejects>float(t["max_reject_rate"]): reasons.append("ORDER_REJECT_RATE")
    base_lat=float(baseline.get("execution_latency_p95_ms") or 0)
    lat=float(metrics.get("execution_latency_p95_ms") or 0)
    if base_lat>0 and lat/base_lat>float(t["max_latency_ratio"]): reasons.append("EXECUTION_LATENCY")
    slip=float(metrics.get("slippage_bps") or 0)-float(baseline.get("slippage_bps") or 0)
    if slip>float(t["max_slippage_delta_bps"]): reasons.append("SLIPPAGE_REGRESSION")
    if int(metrics.get("safety_events",0))>0: reasons.append("SAFETY_EVENT")
    if metrics.get("ledger_ok") is not True: reasons.append("LEDGER_NOT_OK")
    if metrics.get("reconciliation_ok") is not True: reasons.append("RECONCILIATION_NOT_OK")
    return {"promote":not reasons,"rollback":bool(reasons),"reasons":reasons,"automatic_live_promotion":False}
