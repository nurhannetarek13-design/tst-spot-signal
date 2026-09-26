"""Live-data capacity gate. Missing depth/flow is UNKNOWN and blocks new entries."""
from __future__ import annotations
from deep_readiness import safe_capacity
def evaluate(payload):
    try:
        order=float(payload.get("quote_amount_usdt") or 0)
        depth=float(payload["depth_near_touch_usdt"]); flow=float(payload["recent_trade_flow_usdt"])
        spread=float(payload["spread_bps"]); vol=float(payload["short_volatility"])
    except Exception:
        return {"passed":False,"status":"CAPACITY_UNKNOWN","reason":"LIVE_LIQUIDITY_FIELDS_MISSING"}
    row=safe_capacity(order_usdt=order,bid_ask_depth_usdt=depth,recent_flow_usdt=flow,spread_bps=spread,volatility=vol)
    return {"passed":bool(row.get("within_capacity")),"status":"CAPACITY_PASS" if row.get("within_capacity") else "CAPACITY_REJECT",**row}
