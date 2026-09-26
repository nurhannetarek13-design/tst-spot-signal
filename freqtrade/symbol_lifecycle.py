"""Cached Spot symbol lifecycle metadata with fail-closed filter validation."""
from __future__ import annotations
from decimal import Decimal
import time
from production_safety_kernel import D,floor_step

class SymbolMetadataCache:
    def __init__(self,ttl_sec=300):
        self.ttl=float(ttl_sec); self.rows={}; self.updated_at=0.0
    def refresh(self,exchange_info:dict,now=None):
        now=float(now if now is not None else time.time()); rows={}
        for s in exchange_info.get("symbols") or []:
            if str(s.get("status"))!="TRADING" or not s.get("isSpotTradingAllowed",True): continue
            filters={x.get("filterType"):x for x in s.get("filters") or []}
            lot=filters.get("LOT_SIZE") or {}; price=filters.get("PRICE_FILTER") or {}
            notional=filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
            try:
                row={"symbol":str(s["symbol"]),"tick_size":D(price["tickSize"]),"step_size":D(lot["stepSize"]),
                     "min_qty":D(lot["minQty"]),"max_qty":D(lot["maxQty"]),
                     "min_notional":D(notional.get("minNotional","0"))}
            except Exception: continue
            if row["tick_size"]<=0 or row["step_size"]<=0: continue
            rows[row["symbol"]]=row
        self.rows=rows; self.updated_at=now
    def get(self,symbol,now=None):
        now=float(now if now is not None else time.time())
        if not self.rows or now-self.updated_at>self.ttl: raise RuntimeError("SYMBOL_METADATA_STALE")
        row=self.rows.get(str(symbol).upper())
        if not row: raise RuntimeError("SYMBOL_NOT_TRADABLE")
        return row
    def normalize(self,symbol,price,qty,now=None):
        r=self.get(symbol,now); p=floor_step(price,r["tick_size"]); q=floor_step(qty,r["step_size"])
        if q<r["min_qty"] or q>r["max_qty"]: raise RuntimeError("LOT_FILTER")
        if p*q<r["min_notional"]: raise RuntimeError("MIN_NOTIONAL")
        return {"price":p,"quantity":q,"notional":p*q,"metadata_updated_at":self.updated_at}
