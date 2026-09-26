"""Runtime deep-readiness state collector. Diagnostic/fail-closed; never authorizes live."""
from __future__ import annotations
import json,os,time
from pathlib import Path
import market_context
import trade_state
from deep_readiness import regime_transition, dynamic_portfolio_exposure, portfolio_stress
from recovery_security_contracts import snapshot

PATH=Path(os.getenv("TST_DEEP_READINESS_STATE","/data/tst_deep_readiness_state.json"))
BACKUP_DIR=Path(os.getenv("TST_DR_BACKUP_DIR","/data/dr_backups"))
POLL=max(30,int(os.getenv("DEEP_READINESS_POLL_SEC","60")))
BACKUP_EVERY=max(300,int(os.getenv("DR_BACKUP_EVERY_SEC","900")))

def _atomic(row):
    PATH.parent.mkdir(parents=True,exist_ok=True); t=PATH.with_suffix(".tmp")
    t.write_text(json.dumps(row,separators=(",",":"),default=str),encoding="utf-8"); os.replace(t,PATH)

def run_once(previous=None,last_backup=0):
    snap=market_context.load_snapshot()
    cur={"label":snap.get("regime"),"confidence":1.0 if snap else 0.0,
         "realized_vol":((snap.get("btc") or {}).get("volatility_ratio")),"btc_return":((snap.get("btc") or {}).get("r1h"))}
    prev=(previous or {}).get("regime_features")
    transition=regime_transition(prev,cur) if prev else {"state":"REGIME_UNCERTAIN","reasons":["WARMUP"],"size_multiplier":"0"}
    now=time.time(); backup=None
    if now-last_backup>=BACKUP_EVERY:
        try: backup=snapshot("/data",str(BACKUP_DIR)); last_backup=now
        except Exception as exc: backup={"error":type(exc).__name__}
    positions=trade_state.open_positions()
    portfolio_rows=[]
    for p in positions:
        entry=float(p.get("entry") or 0); stop=float(p.get("stop") or 0); qty=float(p.get("quantity") or 0)
        portfolio_rows.append({"symbol":p.get("symbol"),"risk_usdt":max(0,entry-stop)*qty,"notional_usdt":entry*qty,"btc_beta":float(p.get("btc_beta") or 1)})
    # Runtime blocks on missing factor history rather than pretending correlations are zero.
    factor_ready=all(isinstance(p.get("return_history"),list) and len(p.get("return_history"))>=5 for p in positions) if positions else True
    returns={str(p.get("symbol")):p.get("return_history") or [] for p in positions}
    corr=dynamic_portfolio_exposure(portfolio_rows,returns) if factor_ready else {"state":"UNKNOWN","effective_portfolio_risk_usdt":None}
    stress=portfolio_stress(portfolio_rows,{"BTC_-1":{"btc_move":-.01},"BTC_-3":{"btc_move":-.03,"spread_mult":3,"slippage_mult":4},"BTC_-5":{"btc_move":-.05,"alt_liquidity_change":-.4,"spread_mult":3,"slippage_mult":4}})
    row={"updated_at":now,"regime_features":cur,"regime_transition":transition,"last_backup":backup,
         "portfolio_factor_ready":factor_ready,"portfolio_correlation":corr,"portfolio_stress":stress,
         "allow_new_trade":bool(snap) and transition.get("state")=="REGIME_STABLE" and factor_ready,"may_authorize_live":False}
    _atomic(row); return row,last_backup

def main():
    previous={}; last=0
    while True:
        try: previous,last=run_once(previous,last)
        except Exception as exc: print(f"[deep-readiness-runtime] {type(exc).__name__}:{str(exc)[:160]}",flush=True)
        time.sleep(POLL)
if __name__=="__main__": main()
