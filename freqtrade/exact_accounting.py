"""Exact fill accounting, residual inventory classification and execution toxicity."""
from __future__ import annotations
from decimal import Decimal, InvalidOperation

D=lambda x: Decimal(str(x or "0"))

def fill_accounting(*, buy_fills, sell_fills, quote_asset="USDT", fee_fx=None):
    fee_fx=fee_fx or {}
    bq=bn=sq=sn=Decimal("0"); fees_quote=Decimal("0"); unknown=[]
    def consume(rows,is_buy):
        nonlocal bq,bn,sq,sn,fees_quote
        for f in rows or []:
            qty=D(f.get("qty")); price=D(f.get("price")); quote=qty*price
            if qty<=0 or price<=0: raise ValueError("INVALID_FILL")
            if is_buy: bq+=quote; bn+=qty
            else: sq+=quote; sn+=qty
            fee=D(f.get("commission")); asset=str(f.get("commission_asset") or quote_asset)
            if fee:
                if asset==quote_asset: fees_quote+=fee
                elif asset in fee_fx: fees_quote+=fee*D(fee_fx[asset])
                else: unknown.append({"asset":asset,"amount":str(fee)})
    consume(buy_fills,True); consume(sell_fills,False)
    matched=min(bn,sn)
    buy_vwap=bq/bn if bn else Decimal("0"); sell_vwap=sq/sn if sn else Decimal("0")
    gross=(sell_vwap-buy_vwap)*matched
    net=None if unknown else gross-fees_quote
    residual=bn-sn
    return {"buy_qty":bn,"sell_qty":sn,"matched_qty":matched,"buy_vwap":buy_vwap,"sell_vwap":sell_vwap,
            "gross_pnl_quote":gross,"fees_quote":fees_quote,"net_pnl_quote":net,"fee_unknown":unknown,
            "residual_inventory_qty":residual,"exact":not unknown}

def residual_inventory(quantity, *, step_size, min_qty, price, min_notional):
    q=D(quantity); step=D(step_size); mn=D(min_qty); px=D(price); notion=D(min_notional)
    tradable=q>=mn and q*px>=notion and step>0
    return {"quantity":q,"notional":q*px,"classification":"TRADABLE_RESIDUAL" if tradable else "DUST",
            "blocks_reconciliation":False,"requires_accounting":q!=0}

def fill_toxicity(fills, *, threshold_bps=5):
    rows=[]
    for f in fills or []:
        px=D(f.get("fill_price")); side=str(f.get("side") or "BUY").upper()
        obs={}
        toxic=False
        for horizon in ("100ms","500ms","1s","5s"):
            later=f.get("price_"+horizon)
            if later is None or px<=0: continue
            move=(D(later)/px-1)*D(10000)
            adverse=-move if side=="BUY" else move
            obs[horizon]=adverse
            toxic=toxic or adverse>D(threshold_bps)
        rows.append({"order_id":f.get("order_id"),"adverse_bps":obs,"toxic":toxic})
    return {"fills":rows,"toxic_rate":sum(x["toxic"] for x in rows)/len(rows) if rows else None,"diagnostic_only":True}

def queue_decision(*, queue_ahead_usdt, trade_through_usdt_per_sec, cancellation_rate, age_sec, max_wait_sec=3):
    q=max(Decimal("0"),D(queue_ahead_usdt)); rate=max(Decimal("0"),D(trade_through_usdt_per_sec))
    cancel=max(Decimal("0"),min(Decimal("1"),D(cancellation_rate)))
    effective=q*(Decimal("1")-cancel)
    eta=effective/rate if rate>0 else None
    if eta is None: decision="ABANDON"
    elif eta<=D(max_wait_sec): decision="WAIT"
    elif D(age_sec)>=D(max_wait_sec): decision="CANCEL_REPRICE_OR_CROSS"
    else: decision="WAIT"
    return {"effective_queue_ahead_usdt":effective,"expected_fill_seconds":eta,"decision":decision}
