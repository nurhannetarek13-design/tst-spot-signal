"""End-to-end latency attribution with deterministic stage budgets."""
from __future__ import annotations
STAGES=("exchange_event","ingestion","feature","strategy","risk","execution_request","binance_ack","fill")
def latency_trace(timestamps, budgets_ms=None):
    budgets_ms=budgets_ms or {}; spans={}; violations=[]
    prev=None
    for stage in STAGES:
        if stage not in timestamps: continue
        t=float(timestamps[stage])
        if prev is not None:
            name=prev[0]+"->"+stage; ms=max(0,(t-prev[1])*1000); spans[name]=ms
            if name in budgets_ms and ms>float(budgets_ms[name]): violations.append(name)
        prev=(stage,t)
    vals=list(timestamps.values())
    total=max(vals)-min(vals) if len(vals)>=2 else 0
    return {"spans_ms":spans,"total_ms":total*1000,"budget_violations":violations,"degraded":bool(violations)}
