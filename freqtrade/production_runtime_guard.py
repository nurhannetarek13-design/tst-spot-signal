"""Continuous production guard: clock, USDT health, resources and readiness state."""
from __future__ import annotations
import json, os, shutil, time, urllib.request
from pathlib import Path
import production_runtime_hardening as h

STATE=Path(os.getenv("TST_PRODUCTION_GUARD_STATE","/data/tst_production_guard.json"))
INTERVAL=max(2,float(os.getenv("PRODUCTION_GUARD_INTERVAL_SEC","5")))

def _get(url):
 with urllib.request.urlopen(url,timeout=5) as r:return json.loads(r.read())

def sample():
 now_ms=int(time.time()*1000)
 try:
  ex=int(_get("https://api.binance.com/api/v3/time")["serverTime"]); clock=h.clock_drift(local_ms=now_ms,exchange_ms=ex)
 except Exception as e: clock={"status":"UNKNOWN","allow_microstructure_entries":False,"error":type(e).__name__}
 try:
  px=float(_get("https://api.binance.com/api/v3/ticker/bookTicker?symbol=USDCUSDT")["bidPrice"])
  usdt_usd=1/px if px>0 else 0
  q=h.quote_asset_risk(usdt_usd=usdt_usd,spread_bps=0,depth_usd=1_000_000)
 except Exception as e:q={"mode":"UNKNOWN","allow_new_entries":False,"error":type(e).__name__}
 load=os.getloadavg()[0] if hasattr(os,"getloadavg") else 0
 cpu=max(0,min(100,load/max(1,os.cpu_count() or 1)*100))
 try:
  pages=os.sysconf("SC_PHYS_PAGES"); avail=os.sysconf("SC_AVPHYS_PAGES"); ram=(1-avail/pages)*100
 except Exception: ram=0
 disk=shutil.disk_usage("/data"); disk_pct=(disk.used/disk.total*100) if disk.total else 100
 infra=h.infrastructure_backpressure(cpu_pct=cpu,ram_pct=ram,disk_pct=disk_pct,db_pool_pct=0,backlog=0,worker_lag_ms=0)
 checks={"reconcile":Path("/data/reconciliation_ok").exists(),"l2_synced":Path("/data/tst_deep_readiness_state.json").exists(),"indicators_warm":Path("/data/tst_deep_readiness_state.json").exists(),"risk_loaded":Path("/data/trade_state.json").exists(),"metadata_current":True,"clock_ok":clock.get("status")=="OK","quote_asset_ok":q.get("mode")=="NORMAL"}
 ready=h.startup_readiness(checks)
 row={"ts":time.time(),"clock":clock,"quote_asset":q,"infrastructure":infra,"readiness":ready,"allow_new_entries":ready["ready"] and infra["allow_new_entries"] and q.get("allow_new_entries",False) and clock.get("allow_microstructure_entries",False),"live_authorized":False}
 tmp=STATE.with_suffix(".tmp");tmp.write_text(json.dumps(row,separators=(",",":")));os.replace(tmp,STATE);return row

if __name__=="__main__":
 while True:
  try: print("[production-guard]",sample(),flush=True)
  except Exception as e: print("[production-guard] fail-closed",type(e).__name__,e,flush=True)
  time.sleep(INTERVAL)
