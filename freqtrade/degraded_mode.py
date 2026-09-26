"""Fail-closed exchange degraded-mode matrix and operational emergency controls."""
from __future__ import annotations
MODES={
 "NORMAL":{"new_entries":True,"exits":True,"reconcile":True,"cancel":True},
 "MARKET_WS_DOWN":{"new_entries":False,"exits":True,"reconcile":True,"cancel":True},
 "USER_STREAM_DOWN":{"new_entries":False,"exits":True,"reconcile":True,"cancel":True},
 "REST_DOWN":{"new_entries":False,"exits":False,"reconcile":False,"cancel":False},
 "DATABASE_DEGRADED":{"new_entries":False,"exits":True,"reconcile":True,"cancel":True},
 "HIGH_RATE_LIMIT":{"new_entries":False,"exits":True,"reconcile":True,"cancel":True},
 "EXTREME_LATENCY":{"new_entries":False,"exits":True,"reconcile":True,"cancel":True},
 "MAINTENANCE":{"new_entries":False,"exits":True,"reconcile":True,"cancel":True},
 "UNKNOWN":{"new_entries":False,"exits":False,"reconcile":True,"cancel":False},
}
def degraded_policy(health):
    if not isinstance(health,dict): mode="UNKNOWN"
    elif health.get("db_ok") is False: mode="DATABASE_DEGRADED"
    elif health.get("rest_ok") is False: mode="REST_DOWN"
    elif health.get("user_stream_ok") is False: mode="USER_STREAM_DOWN"
    elif health.get("market_ws_ok") is False: mode="MARKET_WS_DOWN"
    elif health.get("maintenance") is True: mode="MAINTENANCE"
    elif float(health.get("rate_limit_ratio") or 0)>=.8: mode="HIGH_RATE_LIMIT"
    elif float(health.get("latency_ms") or 0)>=1000: mode="EXTREME_LATENCY"
    else: mode="NORMAL"
    return {"mode":mode,**MODES[mode],"leave_exchange_protection_intact":mode!="NORMAL"}

CONTROL_MODES={"RUN","PAUSE_NEW_ENTRIES","PROTECTION_ONLY","RECONCILE_ONLY","READ_ONLY","EXIT_ALL"}
def emergency_control(command, *, command_id, seen_ids, authenticated=False):
    if not authenticated:return {"accepted":False,"reason":"UNAUTHENTICATED"}
    if not command_id or command_id in set(seen_ids or []):return {"accepted":False,"reason":"DUPLICATE_OR_MISSING_ID"}
    cmd=str(command).upper()
    if cmd not in CONTROL_MODES:return {"accepted":False,"reason":"UNKNOWN_COMMAND"}
    return {"accepted":True,"command":cmd,"command_id":command_id,"audit_required":True}
