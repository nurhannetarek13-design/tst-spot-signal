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
