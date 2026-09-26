import pytest
from ready_bot.trade_outcome_intelligence import attribute_closed_trade, aggregate_attributions, attach_new_closed_trade_attributions

def test_rejects_open_trade():
    with pytest.raises(ValueError):
        attribute_closed_trade({"status":"OPEN","symbol":"BTCUSDT"})

def test_loss_attribution_is_deterministic_and_fail_closed():
    row=attribute_closed_trade({
        "status":"CLOSED","closed_at":"2026-09-26T00:00:00Z","symbol":"ABCUSDT",
        "setup_type":"BREAKOUT_RETEST","regime":"RISK_ON","pnl_usdt":-0.4,
        "planned_entry":1.0,"entry":1.002,"failed_breakout":True,"btc_shock":True,
        "quality_degraded":True,"exit_reason":"FAILED_BREAKOUT",
    })
    assert row["outcome"]=="LOSS"
    assert {"FAILED_BREAKOUT","LATE_ENTRY","BTC_SHOCK","QUALITY_DEGRADED"} <= set(row["loss_factors"])
    assert row["diagnostic_only"] is True
    assert row["may_authorize_live"] is False
    assert row["may_increase_size"] is False

def test_winner_factors():
    row=attribute_closed_trade({
        "status":"CLOSED","closed_at":"x","symbol":"SOLUSDT","setup_type":"MOMENTUM_IGNITION",
        "pnl_usdt":1.2,"mfe_r":2.1,"strong_relative_strength":True,
        "volume_expansion":True,"cvd_continuation":True,"structure_intact":True,
    })
    assert row["outcome"]=="WIN"
    assert "WINNER_EXTENSION_AVAILABLE" in row["win_factors"]
    assert row["loss_factors"]==[]

def test_aggregation_needs_sample_before_evidence_ready():
    rows=[attribute_closed_trade({"status":"CLOSED","closed_at":"x","setup_type":"PULLBACK_CONTINUATION","regime":"RISK_ON","pnl_usdt":1 if i%2 else -1,"exit_reason":"STOP"}) for i in range(10)]
    out=aggregate_attributions(rows,min_sample=30)
    g=out["groups"]["PULLBACK_CONTINUATION|RISK_ON"]
    assert g["trades"]==10
    assert g["evidence_ready"] is False
    assert out["may_authorize_live"] is False


def test_closed_trade_lifecycle_is_idempotent_and_evidence_only():
    state={"closed_trades":[{
        "symbol":"ABCUSDT","strategy":"BREAKOUT_RETEST","entry":1.0,"exit":0.99,
        "pnl_usdt":-0.1,"reason":"FAILED_BREAKOUT","opened_at":"a","closed_at":"b"
    }]}
    first=attach_new_closed_trade_attributions(state,0,min_sample=2)
    assert len(first["attributions"])==1
    assert first["attributions"][0]["exit_reason"]=="FAILED_BREAKOUT"
    assert "FAILED_BREAKOUT" in first["attributions"][0]["loss_factors"]
    assert first["evidence"]["groups"]["BREAKOUT_RETEST|UNKNOWN"]["evidence_ready"] is False
    again=attach_new_closed_trade_attributions(state,0,min_sample=2)
    assert len(again["attributions"])==1
    assert again["may_authorize_live"] is False
    assert again["may_increase_size"] is False

def test_new_close_only_does_not_recount_history():
    state={"closed_trades":[
        {"symbol":"AUSDT","strategy":"X","pnl_usdt":1,"reason":"TARGET","opened_at":"1","closed_at":"2"},
        {"symbol":"BUSDT","strategy":"Y","pnl_usdt":-1,"reason":"STOP","opened_at":"3","closed_at":"4"},
    ]}
    store=attach_new_closed_trade_attributions(state,1,min_sample=1)
    assert len(store["attributions"])==1
    assert store["attributions"][0]["symbol"]=="BUSDT"
