"""Persist queue and post-fill execution telemetry for runtime toxicity diagnostics."""
from __future__ import annotations
import json,os,time
from pathlib import Path
from exact_accounting import queue_decision, fill_toxicity
PATH=Path(os.getenv("TST_EXECUTION_TELEMETRY","/data/tst_execution_telemetry.jsonl"))
def _append(row):
 PATH.parent.mkdir(parents=True,exist_ok=True)
 with PATH.open("a",encoding="utf-8") as f:f.write(json.dumps(row,separators=(",",":"),default=str)+"\n")
def record_queue(*,symbol,order_id,queue_ahead_usdt,trade_through_usdt_per_sec,cancellation_rate,age_sec):
 d=queue_decision(queue_ahead_usdt=queue_ahead_usdt,trade_through_usdt_per_sec=trade_through_usdt_per_sec,cancellation_rate=cancellation_rate,age_sec=age_sec)
 row={"type":"QUEUE","ts":time.time(),"symbol":symbol,"order_id":order_id,**d};_append(row);return row
def record_post_fill(*,symbol,order_id,side,fill_price,prices):
 fill={"order_id":order_id,"side":side,"fill_price":fill_price,**{"price_"+k:v for k,v in (prices or {}).items()}}
 d=fill_toxicity([fill]);row={"type":"POST_FILL","ts":time.time(),"symbol":symbol,**d["fills"][0]};_append(row);return row
