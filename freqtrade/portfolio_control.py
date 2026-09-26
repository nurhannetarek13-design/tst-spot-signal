"""Strategy-isolated virtual books and conservative capital allocation."""
from __future__ import annotations
from decimal import Decimal
from production_safety_kernel import D

def virtual_books(positions:list[dict], closed:list[dict]|None=None):
    books={}
    for p in list(positions or [])+list(closed or []):
        strategy=str(p.get("strategy") or p.get("strategy_version") or "UNKNOWN")
        b=books.setdefault(strategy,{"open_positions":0,"closed_trades":0,"realized_pnl_usdt":Decimal("0"),"notional_usdt":Decimal("0")})
        status=str(p.get("status") or "").upper()
        if status=="CLOSED":
            b["closed_trades"]+=1; b["realized_pnl_usdt"]+=D(p.get("realized_pnl_usdt",0))
        else:
            b["open_positions"]+=1
            b["notional_usdt"]+=D(p.get("entry",0))*D(p.get("quantity",0))
    return books

def allocate_capital(candidates:list[dict], available_usdt, *, max_total_usdt, max_per_trade_usdt, max_positions=3):
    """Rank only eligible opportunities; never exceeds hard caps."""
    available=min(D(available_usdt),D(max_total_usdt)); cap=D(max_per_trade_usdt)
    eligible=[]
    for c in candidates or []:
        if c.get("eligible") is not True: continue
        ev=D(c.get("net_ev",0)); conf=D(c.get("confidence",0)); liq=D(c.get("liquidity_score",0))
        vol=max(D(c.get("volatility",1)),Decimal("0.000001"))
        corr_penalty=max(Decimal("0"),min(Decimal("1"),D(c.get("correlation_penalty",0))))
        score=(max(ev,Decimal("0"))*max(conf,Decimal("0"))*max(liq,Decimal("0"))/vol)*(Decimal("1")-corr_penalty)
        if score>0: eligible.append((score,c))
    eligible.sort(key=lambda x:(-x[0],str(x[1].get("symbol") or "")))
    out=[]; remaining=available
    for score,c in eligible[:int(max_positions)]:
        if remaining<=0: break
        requested=D(c.get("requested_usdt",cap)); amount=min(cap,requested,remaining)
        if amount<=0: continue
        out.append({"symbol":c.get("symbol"),"strategy":c.get("strategy"),"allocated_usdt":amount,"allocation_score":score})
        remaining-=amount
    return {"allocations":out,"allocated_usdt":available-remaining,"remaining_usdt":remaining,"fail_closed":True}
