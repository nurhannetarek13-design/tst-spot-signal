#!/usr/bin/env python3
"""Post-trade diagnostics by UTC liquidity session.

Diagnostic only. No session may be auto-blocked until it has a sufficient
closed-trade sample and a separate review approves a policy change.
"""
from __future__ import annotations
import argparse,json,pathlib,statistics


def summarize(rows,min_sample=30):
    groups={}
    for r in rows:
        ctx=r.get("entry_context") or {}
        session=ctx.get("session") or "UNKNOWN"
        groups.setdefault(session,[]).append(r)
    out={}
    for session,rs in sorted(groups.items()):
        vals=[float(x.get("pnl_usdt") or 0) for x in rs]
        wins=sum(v for v in vals if v>0);losses=-sum(v for v in vals if v<0)
        sl=[float((x.get("entry_context") or {}).get("estimated_entry_slippage_bps")) for x in rs
            if (x.get("entry_context") or {}).get("estimated_entry_slippage_bps") is not None]
        lat=[float((x.get("entry_context") or {}).get("decision_latency_ms")) for x in rs
             if (x.get("entry_context") or {}).get("decision_latency_ms") is not None]
        out[session]={
            "n":len(rs),
            "diagnosticOnly":len(rs)<int(min_sample),
            "expectancyUSDT":sum(vals)/len(vals) if vals else 0.0,
            "hitRate":sum(v>0 for v in vals)/len(vals) if vals else 0.0,
            "profitFactor":wins/losses if losses>0 else (99.0 if wins>0 else 0.0),
            "medianEntrySlippageBps":statistics.median(sl) if sl else None,
            "medianDecisionLatencyMs":statistics.median(lat) if lat else None,
        }
    return out


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--state",default="ready_bot/indicator_state.json")
    ap.add_argument("--output",default="validation/indicator-v3/session-performance.json")
    a=ap.parse_args()
    state=json.loads(pathlib.Path(a.state).read_text())
    out={
        "engine":"V3_SESSION_PERFORMANCE_DIAGNOSTIC",
        "authorization":"PAPER_SHADOW_ONLY",
        "liveTrading":False,
        "autoSessionBlocking":False,
        "sessions":summarize(state.get("closed_trades") or []),
    }
    p=pathlib.Path(a.output);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(out,indent=2))
    print(json.dumps(out,indent=2))


if __name__=="__main__":
    main()
