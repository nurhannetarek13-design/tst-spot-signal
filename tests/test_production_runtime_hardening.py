import os,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/"freqtrade"))
import production_runtime_hardening as p

def test_ownership_fencing():
 with tempfile.TemporaryDirectory() as d:
  x=p.ExecutionOwnership(Path(d)/"lease",ttl=10)
  a=x.acquire("a",now=100); assert a["ok"]
  assert not x.acquire("b",now=101)["ok"]
  b=x.acquire("b",now=111); assert b["ok"] and b["fencing_token"]>a["fencing_token"]
  assert not x.validate("a",a["fencing_token"],now=112)

def test_atomic_capital():
 with tempfile.TemporaryDirectory() as d:
  x=p.AtomicCapital(Path(d)/"cap")
  assert x.reserve("a",7,10)["ok"]
  assert x.reserve("a",7,10)["status"]=="IDEMPOTENT"
  assert not x.reserve("b",4,10)["ok"]
  x.release("a"); assert x.reserve("b",4,10)["ok"]

def test_readiness_and_shutdown():
 assert not p.startup_readiness({})["ready"]
 checks={x:True for x in p.READINESS_STEPS}; assert p.startup_readiness(checks)["ready"]
 assert not p.shutdown_plan(new_entries_stopped=True,inflight=1,state_persisted=True,protection_verified=True,ownership_released=True)["safe_to_stop"]
 assert p.shutdown_plan(new_entries_stopped=True,inflight=0,state_persisted=True,protection_verified=True,ownership_released=True)["safe_to_stop"]

def test_external_and_conflict():
 assert p.classify_account_activity(client_order_id="x",known_bot_ids={"x"})=="BOT_OWNED"
 assert p.classify_account_activity(client_order_id="manual",known_bot_ids=set())=="EXTERNAL"
 r=p.resolve_strategy_conflict([{"symbol":"BTCUSDT","side":"BUY","strategy":"A","priority":2},{"symbol":"BTCUSDT","side":"SELL","strategy":"B","priority":1}])
 assert len(r["accepted"])==1 and r["rejected"][0]["reason"]=="SELF_TRADE_CONFLICT"

def test_exchange_semantics_and_protection():
 assert p.order_semantics("EXPIRED_IN_MATCH",orig_qty=2,executed_qty=1,prevented_qty=.5)["residual_qty"]==.5
 assert p.order_semantics("ALIEN")["block"]
 r=p.protection_effectiveness(protected_qty=2,position_qty=2,stop_triggered=True,executed_qty=1,expected_stop=100,avg_fill=99)
 assert not r["effective"] and r["action"]=="EMERGENCY_EXIT"

def test_sanity_backpressure_quote_clock():
 assert p.market_sanity(last=100,mid=100,book_ticker=100,vwap=100)["passed"]
 assert p.market_sanity(last=110,mid=100,book_ticker=100,vwap=100)["quarantine"]
 assert not p.infrastructure_backpressure(cpu_pct=95,ram_pct=20,disk_pct=20,db_pool_pct=20,backlog=0,worker_lag_ms=1)["allow_new_entries"]
 assert p.quote_asset_risk(usdt_usd=.98,spread_bps=2,depth_usd=1e7)["mode"]=="PROTECTION_ONLY"
 assert p.clock_drift(local_ms=2000,exchange_ms=0)["status"]=="BLOCK"

def test_durable_queue_and_global_gate():
 with tempfile.TemporaryDirectory() as d:
  q=p.DurableQueue(Path(d)/"q.jsonl"); e=q.append({"event_id":"e"})
  assert q.fail(e,max_attempts=2)["status"]=="RETRY"
  assert q.fail({**e,"attempts":1},max_attempts=2)["status"]=="DEAD_LETTER"
 ready=p.startup_readiness({x:True for x in p.READINESS_STEPS})
 g=p.production_gate(readiness=ready,ownership_ok=True,capital_ok=True,market={"passed":True},infra={"allow_new_entries":True},quote={"allow_new_entries":True},clock={"allow_microstructure_entries":True})
 assert g["allow_entry"]
 g=p.production_gate(readiness=ready,ownership_ok=False,capital_ok=True,market={"passed":True},infra={"allow_new_entries":True},quote={"allow_new_entries":True},clock={"allow_microstructure_entries":True})
 assert not g["allow_entry"]
