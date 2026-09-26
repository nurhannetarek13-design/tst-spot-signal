from decimal import Decimal
import importlib.util
from pathlib import Path

P=Path(__file__).resolve().parents[1]/"freqtrade"/"production_safety_kernel.py"
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
    z=importlib.util.spec_from_file_location(name,q); m=importlib.util.module_from_spec(z); z.loader.exec_module(m); return m

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
