#!/usr/bin/env python3
"""Independent read-only watchdog. Never opens positions or assumes unknown outcomes."""
import json,os,time
from pathlib import Path
from production_safety_kernel import assert_invariants

STATE=Path(os.getenv("TST_TRADE_STATE_PATH","/data/tst_live_positions.json"))
HEALTH=Path(os.getenv("TST_EXECUTION_HEALTH_PATH","/data/tst_execution_health.json"))
OUT=Path(os.getenv("TST_WATCHDOG_STATUS_PATH","/data/tst_watchdog_status.json"))
MAX_STALE=float(os.getenv("WATCHDOG_MAX_HEALTH_STALE_SEC","180"))

def _read(path):
    try:
        x=json.loads(path.read_text()); return x if isinstance(x,dict) else {}
    except Exception:return {}

def check():
    now=time.time(); state=_read(STATE); health=_read(HEALTH)
    inv=assert_invariants(state,max_risk_usdt=os.getenv("MAX_DAILY_LOSS_USDT","2"))
    last=float(health.get("last_ok_at") or 0)
    reasons=list(inv["violations"])
    if not health: reasons.append("HEALTH_MISSING")
    elif now-last>MAX_STALE: reasons.append("HEALTH_STALE")
    row={"ok":not reasons,"allow_new_trade":False if reasons else True,"checked_at":now,
         "reasons":sorted(set(reasons)),"invariants":inv}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(row,separators=(",",":")))
    return row

def main():
    poll=max(5,int(os.getenv("WATCHDOG_POLL_SEC","15")))
    while True:
        row=check()
        print("[watchdog] "+json.dumps(row,separators=(",",":")),flush=True)
        time.sleep(poll)
if __name__=="__main__": main()
