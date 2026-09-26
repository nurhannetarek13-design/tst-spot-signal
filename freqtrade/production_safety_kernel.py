"""Production safety kernel: explicit FSM, invariants, ledger and version stamps.

No exchange writes. This layer is fail-closed and can only BLOCK execution.
"""
from __future__ import annotations
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from hashlib import sha256
import json, os, time

STATES=("DETECTED","VALIDATED","ARMED","ORDER_SENT","PARTIAL","FILLED","PROTECTED","EXITING","CLOSED","UNKNOWN")
ALLOWED={
 "DETECTED":{"VALIDATED","CLOSED"},
 "VALIDATED":{"ARMED","CLOSED"},
 "ARMED":{"ORDER_SENT","CLOSED"},
 "ORDER_SENT":{"PARTIAL","FILLED","UNKNOWN","CLOSED"},
 "PARTIAL":{"PARTIAL","FILLED","UNKNOWN","CLOSED"},
 "FILLED":{"PROTECTED","EXITING","UNKNOWN"},
 "PROTECTED":{"EXITING","UNKNOWN"},
 "EXITING":{"PARTIAL","CLOSED","UNKNOWN"},
 "UNKNOWN":{"ORDER_SENT","PARTIAL","FILLED","PROTECTED","EXITING","CLOSED"},
 "CLOSED":set(),
}

def transition(current:str,target:str)->str:
    current=str(current).upper(); target=str(target).upper()
    if current not in ALLOWED or target not in STATES or target not in ALLOWED[current]:
        raise RuntimeError(f"ILLEGAL_TRADE_TRANSITION:{current}->{target}")
    return target

def D(value)->Decimal:
    try:
        x=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError):
        raise ValueError("INVALID_DECIMAL")
    if not x.is_finite():
        raise ValueError("NONFINITE_DECIMAL")
    return x

def floor_step(value,step):
    v,s=D(value),D(step)
    if v<0 or s<=0: raise ValueError("INVALID_PRECISION_INPUT")
    return (v/s).to_integral_value(rounding=ROUND_DOWN)*s

def assert_invariants(state:dict, *, max_risk_usdt=Decimal("2")):
    violations=[]
    positions=(state or {}).get("positions") or {}
    seen_symbols=set()
    total_risk=Decimal("0")
    for key,p in positions.items():
        if not isinstance(p,dict): violations.append(f"INVALID_POSITION:{key}"); continue
        status=str(p.get("status") or "").upper()
        if status=="CLOSED": continue
        symbol=str(p.get("symbol") or "").upper()
        if not symbol: violations.append(f"MISSING_SYMBOL:{key}")
        elif symbol in seen_symbols: violations.append(f"DUPLICATE_POSITION:{symbol}")
        else: seen_symbols.add(symbol)
        try:
            qty=D(p.get("quantity",0)); entry=D(p.get("entry",0)); stop=D(p.get("stop",0))
            if qty<=0: violations.append(f"INVALID_QUANTITY:{key}")
            if status in {"FILLED","PROTECTED","EXITING"} and stop<=0:
                violations.append(f"POSITION_WITHOUT_SL:{key}")
            if entry>0 and stop>0 and entry>stop and qty>0:
                total_risk+=(entry-stop)*qty
        except ValueError:
            violations.append(f"INVALID_FINANCIAL_VALUE:{key}")
    if total_risk>D(max_risk_usdt): violations.append("ACCOUNT_RISK_LIMIT_EXCEEDED")
    reservations=(state or {}).get("reservations") or {}
    if any(str(x.get("status") or "").upper()=="UNKNOWN" for x in reservations.values() if isinstance(x,dict)):
        violations.append("UNKNOWN_ORDER_STATE")
    return {"ok":not violations,"violations":violations,"total_risk_usdt":str(total_risk),"allow_new_trade":not violations}

def append_ledger_event(ledger:list,event:dict):
    prev=ledger[-1]["hash"] if ledger else "GENESIS"
    body={"seq":len(ledger)+1,"ts":event.get("ts",time.time()),**{k:v for k,v in event.items() if k!="hash"},"prev_hash":prev}
    raw=json.dumps(body,sort_keys=True,separators=(",",":"),default=str)
    body["hash"]=sha256(raw.encode()).hexdigest()
    return [*ledger,body]

def verify_ledger(ledger:list)->bool:
    prev="GENESIS"
    for i,row in enumerate(ledger or [],1):
        if row.get("seq")!=i or row.get("prev_hash")!=prev: return False
        body={k:v for k,v in row.items() if k!="hash"}
        raw=json.dumps(body,sort_keys=True,separators=(",",":"),default=str)
        if sha256(raw.encode()).hexdigest()!=row.get("hash"): return False
        prev=row["hash"]
    return True

def version_stamp(config:dict|None=None):
    return {
      "code_commit":os.getenv("GIT_COMMIT_SHA","UNKNOWN"),
      "strategy_version":os.getenv("STRATEGY_VERSION","UNKNOWN"),
      "risk_version":os.getenv("RISK_VERSION","UNKNOWN"),
      "config_hash":sha256(json.dumps(config or {},sort_keys=True,separators=(",",":")).encode()).hexdigest()[:16],
    }
