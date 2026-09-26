from decimal import Decimal
import importlib.util
import sys
from pathlib import Path

FREQ=Path(__file__).resolve().parents[1]/"freqtrade"
if str(FREQ) not in sys.path: sys.path.insert(0,str(FREQ))
P=FREQ/"production_safety_kernel.py"
spec=importlib.util.spec_from_file_location("psk",P); psk=importlib.util.module_from_spec(spec); spec.loader.exec_module(psk)

def test_fsm_blocks_illegal_transition():
    assert psk.transition("DETECTED","VALIDATED")=="VALIDATED"
    try: psk.transition("DETECTED","FILLED")
    except RuntimeError: pass
    else: raise AssertionError("illegal transition accepted")

def test_unknown_order_blocks_new_trade():
    state={"positions":{},"reservations":{"x":{"status":"UNKNOWN"}}}
    x=psk.assert_invariants(state)
    assert not x["ok"] and not x["allow_new_trade"]
    assert "UNKNOWN_ORDER_STATE" in x["violations"]

def test_unprotected_and_duplicate_positions_fail_closed():
    state={"positions":{
      "1":{"status":"FILLED","symbol":"SOLUSDT","entry":"100","stop":"0","quantity":"0.01"},
      "2":{"status":"PROTECTED","symbol":"SOLUSDT","entry":"100","stop":"99","quantity":"0.01"},
    }}
    x=psk.assert_invariants(state)
    assert any(v.startswith("POSITION_WITHOUT_SL") for v in x["violations"])
    assert "DUPLICATE_POSITION:SOLUSDT" in x["violations"]

def test_decimal_step_is_exact():
    assert psk.floor_step("1.234567","0.001")==Decimal("1.234")

def test_ledger_detects_tampering():
    ledger=psk.append_ledger_event([],{"kind":"FILL","fee":"0.001"})
    ledger=psk.append_ledger_event(ledger,{"kind":"PROTECTION","order_id":7})
    assert psk.verify_ledger(ledger)
    ledger[0]["fee"]="99"
    assert not psk.verify_ledger(ledger)

def test_version_stamp_is_stable_for_same_config():
    a=psk.version_stamp({"b":2,"a":1}); b=psk.version_stamp({"a":1,"b":2})
    assert a["config_hash"]==b["config_hash"]


def _load(name):
    q=Path(__file__).resolve().parents[1]/"freqtrade"/f"{name}.py"
    z=importlib.util.spec_from_file_location(name,q); m=importlib.util.module_from_spec(z); sys.modules[name]=m; z.loader.exec_module(m); return m

def test_sequence_guard_rejects_old_and_gaps_until_resync():
    m=_load("event_integrity"); g=m.SequenceGuard()
    assert g.accept("SOL",10)["accepted"]
    assert g.accept("SOL",10)["action"]=="DROP_OLD"
    assert g.accept("SOL",12,prev_seq=9)["action"]=="GAP_RESYNC"
    assert g.accept("SOL",13,prev_seq=12)["action"]=="RESYNC_REQUIRED"
    g.resync("SOL",20)
    assert g.accept("SOL",21,prev_seq=20)["accepted"]

def test_deterministic_replay_preserves_equal_timestamp_source_order():
    m=_load("event_integrity"); seen=[]
    events=[{"ts_ms":2,"type":"c"},{"ts_ms":1,"type":"a"},{"ts_ms":1,"type":"b"}]
    trace=m.deterministic_replay(events,lambda e: seen.append(e["type"]) or e["type"])
    assert seen==["a","b","c"] and [x["source_index"] for x in trace]==[1,2,0]

def test_rate_budget_preserves_critical_reserve():
    m=_load("event_integrity"); b=m.RateLimitBudgeter(capacity=5,reserve_critical=2)
    assert b.allow("ANALYTICS",3)
    assert not b.allow("ANALYTICS",1)
    assert b.allow("RECONCILIATION",2)

def test_symbol_metadata_stale_and_decimal_normalization():
    m=_load("symbol_lifecycle"); c=m.SymbolMetadataCache(ttl_sec=10)
    info={"symbols":[{"symbol":"SOLUSDT","status":"TRADING","isSpotTradingAllowed":True,"filters":[
      {"filterType":"PRICE_FILTER","tickSize":"0.01"},
      {"filterType":"LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"1000"},
      {"filterType":"MIN_NOTIONAL","minNotional":"5"}]}]}
    c.refresh(info,now=100)
    x=c.normalize("SOLUSDT","101.239","0.1239",now=105)
    assert str(x["price"])=="101.23" and str(x["quantity"])=="0.123"
    try:c.get("SOLUSDT",now=111)
    except RuntimeError as e: assert str(e)=="SYMBOL_METADATA_STALE"
    else: raise AssertionError("stale metadata accepted")


def test_feature_flag_assignment_is_deterministic():
    m=_load("production_observability"); flags={"CVD_V2":{"enabled":True,"percent":10}}
    assert m.feature_enabled("CVD_V2","sig-1",flags)==m.feature_enabled("CVD_V2","sig-1",flags)
    assert not m.feature_enabled("OFF","sig-1",{"OFF":{"enabled":False,"percent":100}})

def test_observability_and_postmortem():
    m=_load("production_observability"); x=m.Metrics()
    x.inc("rejected_orders"); x.observe("execution_latency_ms",100); x.observe("execution_latency_ms",200)
    snap=x.snapshot(); assert snap["counts"]["rejected_orders"]==1 and snap["samples"]["execution_latency_ms"]["max"]==200
    pm=m.build_postmortem({"type":"SAFETY_EVENT","behavior":"BLOCK","decision":"NO_NEW_TRADE"},
      {"version":{"code_commit":"abc"}},{"expected_behavior":"BLOCK"})
    assert pm["behavior_matched_spec"] is True and pm["diagnostic_only"] is True


def test_strategy_virtual_books_do_not_hide_bad_strategy():
    m=_load("portfolio_control")
    books=m.virtual_books([],[
      {"status":"CLOSED","strategy":"A","realized_pnl_usdt":"2"},
      {"status":"CLOSED","strategy":"B","realized_pnl_usdt":"-3"}])
    assert books["A"]["realized_pnl_usdt"]==Decimal("2")
    assert books["B"]["realized_pnl_usdt"]==Decimal("-3")

def test_capital_allocator_respects_hard_caps():
    m=_load("portfolio_control")
    c=[{"symbol":"A","strategy":"S1","eligible":True,"net_ev":"0.02","confidence":"0.9","liquidity_score":"0.9","volatility":"0.02","requested_usdt":"10"},
       {"symbol":"B","strategy":"S2","eligible":True,"net_ev":"0.01","confidence":"0.8","liquidity_score":"0.8","volatility":"0.02","requested_usdt":"10"}]
    x=m.allocate_capital(c,"20",max_total_usdt="12",max_per_trade_usdt="7",max_positions=3)
    assert x["allocated_usdt"]<=Decimal("12")
    assert all(a["allocated_usdt"]<=Decimal("7") for a in x["allocations"])

def test_chaos_suite_fails_closed():
    m=_load("chaos_safety"); x=m.run_chaos_suite()
    assert x["ok"] is True and x["live_authorized"] is False


def test_canary_guard_rolls_back_on_safety_or_regression():
    m=_load("canary_guard")
    good=m.canary_decision({"signals":100,"api_errors":0,"rejected_orders":0,"execution_latency_p95_ms":100,
      "slippage_bps":2,"safety_events":0,"ledger_ok":True,"reconciliation_ok":True},
      {"execution_latency_p95_ms":100,"slippage_bps":2})
    assert good["promote"] is True and good["automatic_live_promotion"] is False
    bad=m.canary_decision({"signals":100,"api_errors":3,"rejected_orders":0,"execution_latency_p95_ms":100,
      "slippage_bps":2,"safety_events":1,"ledger_ok":True,"reconciliation_ok":True},
      {"execution_latency_p95_ms":100,"slippage_bps":2})
    assert bad["rollback"] is True and {"API_ERROR_RATE","SAFETY_EVENT"} <= set(bad["reasons"])


def test_canary_rolls_back_on_safety_or_execution_regression():
    m=_load("canary_guard")
    baseline={"execution_latency_p95_ms":100,"slippage_bps":2}
    good={"signals":100,"api_errors":0,"rejected_orders":0,"execution_latency_p95_ms":110,"slippage_bps":2.5,
          "safety_events":0,"ledger_ok":True,"reconciliation_ok":True}
    bad=dict(good); bad["safety_events"]=1
    assert m.canary_decision(good,baseline)["promote"] is True
    x=m.canary_decision(bad,baseline)
    assert x["rollback"] is True and x["automatic_live_promotion"] is False
