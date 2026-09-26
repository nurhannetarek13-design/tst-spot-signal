import importlib.util, pathlib
ROOT=pathlib.Path(__file__).resolve().parents[1]
def load(path,name):
 s=importlib.util.spec_from_file_location(name,ROOT/path); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

def test_calibration_abstains_without_evidence_and_buys_only_calibrated():
 m=load("freqtrade/deep_readiness.py","deep")
 rows=[{"score":92,"setup_type":"MOM","regime":"BULL","liquidity_bucket":"HIGH","volatility_bucket":"MID","net_pnl_usdt":1} for _ in range(30)]
 c=m.calibration_table(rows,min_sample=30); key=next(iter(c["groups"]))
 f={"calibration_key":key,"data_confidence":1,"liquidity_confidence":1,"regime_confidence":1,"regime_state":"REGIME_STABLE"}
 assert m.calibrated_decision(f,c)["decision"]=="BUY"
 f["regime_state"]="REGIME_TRANSITION"; assert m.calibrated_decision(f,c)["decision"]=="UNKNOWN"

def test_transition_correlation_stress_and_capacity_fail_closed():
 m=load("freqtrade/deep_readiness.py","deep2")
 assert m.regime_transition({"realized_vol":1,"label":"BULL"},{"realized_vol":2,"label":"BULL","confidence":.9})["state"]=="REGIME_TRANSITION"
 p=[{"symbol":"A","risk_usdt":1,"notional_usdt":10},{"symbol":"B","risk_usdt":1,"notional_usdt":10}]
 r={"A":[1,2,3,4,5],"B":[1,2,3,4,5]}
 assert m.dynamic_portfolio_exposure(p,r)["effective_portfolio_risk_usdt"]>2
 assert m.portfolio_stress(p,{"crash":{"btc_move":-.03,"spread_mult":3,"slippage_mult":4}})["worst_projected_loss_usdt"]>0
 assert m.safe_capacity(order_usdt=100,bid_ask_depth_usdt=100,recent_flow_usdt=100,spread_bps=5,volatility=.02)["fail_closed"]

def test_exact_fill_accounting_dust_queue_toxicity():
 m=load("freqtrade/exact_accounting.py","acct")
 x=m.fill_accounting(buy_fills=[{"qty":"2","price":"10","commission":"0.01","commission_asset":"USDT"}],sell_fills=[{"qty":"2","price":"11","commission":"0.01","commission_asset":"USDT"}])
 assert str(x["gross_pnl_quote"])=="2" and str(x["net_pnl_quote"])=="1.98" and x["exact"]
 assert m.residual_inventory(".001",step_size=".001",min_qty=".01",price=10,min_notional=5)["classification"]=="DUST"
 assert m.queue_decision(queue_ahead_usdt=100,trade_through_usdt_per_sec=0,cancellation_rate=0,age_sec=5)["decision"]=="ABANDON"
 assert m.fill_toxicity([{"fill_price":100,"side":"BUY","price_1s":99.9}],threshold_bps=5)["fills"][0]["toxic"]

def test_degraded_modes_and_controls():
 m=load("freqtrade/degraded_mode.py","deg")
 assert not m.degraded_policy({"market_ws_ok":False,"rest_ok":True,"db_ok":True,"user_stream_ok":True})["new_entries"]
 assert not m.emergency_control("EXIT_ALL",command_id="x",seen_ids=set(),authenticated=False)["accepted"]
 assert m.emergency_control("PROTECTION_ONLY",command_id="x",seen_ids=set(),authenticated=True)["accepted"]

def test_research_integrity_and_false_discovery():
 m=load("research/research_integrity.py","ri")
 assert m.drift_report({"rvol":[.5,.5]},{"rvol":[.9,.1]})["features"]["rvol"]["state"] in {"WARN","BLOCK"}
 f=m.false_discovery_guard([{"p_value":.001},{"p_value":.9}]); assert f["hypotheses_tested"]==2 and f["accepted"]==1
 assert not m.baseline_comparison([1],{"simple":[2]})["complexity_justified"]
 assert m.strategy_dependency({"a":[1,2,3,4,5],"b":[2,4,6,8,10]})["independence_review_required"]

def test_recovery_schema_security_and_latency(tmp_path):
 m=load("freqtrade/recovery_security_contracts.py","rc")
 d=tmp_path/"d"; d.mkdir(); (d/"tst_live_positions.json").write_text("{}")
 x=m.snapshot(d,tmp_path/"b"); assert m.verify_snapshot(x["path"])["ok"]
 assert not m.validate_binance_payload({"a":"x"},{"a":int})["ok"]
 import hashlib,hmac,time
 raw=b"{}"; ts=int(time.time()); nonce="n"; sec="s"; sig=hmac.new(sec.encode(),str(ts).encode()+b"."+nonce.encode()+b"."+raw,hashlib.sha256).hexdigest()
 assert m.verify_signed_webhook(raw,signature=sig,timestamp=ts,nonce=nonce,secret=sec,seen_nonces=set())["ok"]
 l=load("freqtrade/latency_attribution.py","lat")
 assert l.latency_trace({"exchange_event":1,"ingestion":1.2},{"exchange_event->ingestion":100})["degraded"]


def test_spoofing_cancellation_diagnostics():
 m=load("freqtrade/spoofing_diagnostics.py","sp")
 snaps=[]
 for i in range(3):
  snaps += [{"ts_ms":i*2000,"bids":[[100+i,200]],"asks":[]},{"ts_ms":i*2000+500,"bids":[],"asks":[]}]
 x=m.spoofing_diagnostics(snaps,min_wall_usdt=10000,max_lifetime_ms=1000)
 assert x["suspected_spoofing"] and x["fast_cancel_rate"]==1.0


def test_capacity_gate_blocks_missing_live_liquidity():
 import sys
 sys.path.insert(0,str(ROOT/"freqtrade"))
 m=load("freqtrade/capacity_gate.py","cap")
 assert m.evaluate({"quote_amount_usdt":10})["status"]=="CAPACITY_UNKNOWN"
 ok=m.evaluate({"quote_amount_usdt":1,"depth_near_touch_usdt":10000,"recent_trade_flow_usdt":10000,"spread_bps":1,"short_volatility":.001})
 assert ok["passed"]

def test_promotion_integrity_requires_every_research_protection():
 m=load("research/promotion_integrity.py","promo")
 good=m.promotion_gate(
   calibration={"groups":{"x":{"evidence_ready":True}}},
   drift={"block_promotion":False},
   fdr={"hypotheses_tested":10,"accepted":1},
   baselines={"complexity_justified":True},
   dependency={"independence_review_required":False},
   forward={"evidence_pass":True},
   champion={"shadow_pass":True})
 assert good["promotion_allowed"] and not good["automatic_live_authorization"]
 bad=m.promotion_gate(calibration={},drift={},fdr={},baselines={},dependency={},forward={},champion={})
 assert not bad["promotion_allowed"] and len(bad["reasons"])>=6


def test_readiness_audit_never_overclaims_external_dependencies():
 m=load("freqtrade/readiness_audit.py","audit")
 a=m.audit()
 assert a["full"]==15 and a["partial"]==6 and not a["architecture_complete"] and not a["live_authorized"]
 b=m.audit(offsite_dr=True,live_factor_history=True,live_queue_telemetry=True,live_post_fill_telemetry=True,fee_fx_complete=True)\n c=m.audit(offsite_dr=True,live_factor_history=True,live_queue_telemetry=True,live_post_fill_telemetry=True,fee_fx_complete=True,live_l2_spoofing=True)
 assert b["full"]==20 and b["partial"]==1 and not b["architecture_complete"] and not b["live_authorized"]\n assert c["full"]==21 and c["architecture_complete"] and not c["live_authorized"]


def test_execution_telemetry_persists_queue_and_toxicity(tmp_path):
 import sys
 sys.path.insert(0,str(ROOT/"freqtrade"))
 m=load("freqtrade/execution_telemetry.py","tele")
 m.PATH=tmp_path/"tele.jsonl"
 q=m.record_queue(symbol="BTCUSDT",order_id=1,queue_ahead_usdt=10,trade_through_usdt_per_sec=100,cancellation_rate=.1,age_sec=.1)
 assert q["decision"]=="WAIT"
 t=m.record_post_fill(symbol="BTCUSDT",order_id=1,side="BUY",fill_price=100,prices={"100ms":99.9,"500ms":99.8})
 assert t["toxic"] and m.PATH.exists()
