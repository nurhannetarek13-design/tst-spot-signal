"""Deterministic chaos scenarios for production-safety contracts."""
from __future__ import annotations
from production_safety_kernel import assert_invariants
from event_integrity import SequenceGuard

def run_chaos_suite():
    results={}
    # duplicate event
    g=SequenceGuard(); g.accept("user",10)
    results["duplicate_event"]=g.accept("user",10)["action"]=="DROP_OLD"
    # sequence gap
    results["sequence_gap"]=g.accept("user",12,prev_seq=9)["action"]=="GAP_RESYNC"
    # process restart / unknown outcome must block
    results["unknown_after_restart"]=not assert_invariants({"positions":{},"reservations":{"x":{"status":"UNKNOWN"}}})["allow_new_trade"]
    # unprotected fill must block
    results["fill_without_protection"]=not assert_invariants({"positions":{"x":{"status":"FILLED","symbol":"SOLUSDT","entry":"100","stop":"0","quantity":"0.1"}}})["allow_new_trade"]
    # invalid quantity must block
    results["invalid_quantity"]=not assert_invariants({"positions":{"x":{"status":"PROTECTED","symbol":"BTCUSDT","entry":"60000","stop":"59000","quantity":"-1"}}})["allow_new_trade"]
    return {"ok":all(results.values()),"scenarios":results,"live_authorized":False}

if __name__=="__main__":
    import json
    x=run_chaos_suite(); print(json.dumps(x,sort_keys=True))
    raise SystemExit(0 if x["ok"] else 1)
