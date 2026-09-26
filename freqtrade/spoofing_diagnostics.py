"""Cancellation/spoofing diagnostics from ordered L2 snapshots."""
from __future__ import annotations
def spoofing_diagnostics(snapshots, *, min_wall_usdt=10000, max_lifetime_ms=1500):
    born={}; vanished=[]; last={}
    for s in snapshots or []:
        ts=int(s.get("ts_ms") or 0); levels={}
        for side in ("bids","asks"):
            for price,qty in s.get(side) or []:
                notional=float(price)*float(qty)
                key=(side,str(price)); levels[key]=notional
                if notional>=min_wall_usdt and key not in born: born[key]=ts
        for key,notional in last.items():
            if notional>=min_wall_usdt and key not in levels and key in born:
                life=ts-born.pop(key); vanished.append({"side":key[0],"price":key[1],"lifetime_ms":life,"fast_cancel":life<=max_lifetime_ms})
        last=levels
    n=len(vanished); fast=sum(x["fast_cancel"] for x in vanished)
    return {"large_walls_vanished":n,"fast_cancel_rate":fast/n if n else 0.0,"suspected_spoofing":n>=3 and fast/n>=.6,"events":vanished[-100:],"diagnostic_only":True}
