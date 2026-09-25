import math

import ready_bot.indicator_bot as bot


def bars(n=250, drift=0.001, taker=0.60, qv=2_000_000.0):
    out=[]
    p=1.0
    for i in range(n):
        o=p
        p=p*(1+drift)
        h=max(o,p)*1.002
        l=min(o,p)*0.998
        this_qv=qv*(1.7 if i==n-1 else 1.0)
        out.append({"t":i*900000,"o":o,"h":h,"l":l,"c":p,"v":this_qv/p,"qv":this_qv,"tq":this_qv*taker})
    return out


def test_engine_is_indicator_only_and_paper():
    assert bot.CFG["mode"]=="paper"
    assert bot.CFG["engine"]=="INDICATOR_ONLY_V1"
    assert "strategies" not in bot.CFG
    assert bot.CFG["entry"]["min_score"]==90


def test_indicator_score_is_bounded_and_grouped():
    b15=bars(taker=.62)
    b1=bars(drift=.0015,taker=.58)
    b4=bars(drift=.002,taker=.58)
    snap=bot.indicator_snapshot("TESTUSDT",b15,b1,b4,1.0,1.0005,50_000_000)
    assert 0 <= snap["score"] <= 100
    assert set(snap["groups"])=={"trend","momentum","flow","volatility","liquidity"}
    assert snap["spread_bps"] < bot.CFG["entry"]["max_spread_bps"]


def test_low_taker_flow_is_hard_veto():
    b15=bars(taker=.45)
    b1=bars(drift=.0015,taker=.58)
    b4=bars(drift=.002,taker=.58)
    snap=bot.indicator_snapshot("TESTUSDT",b15,b1,b4,1.0,1.0005,50_000_000)
    assert "TAKER_FLOW" in snap["vetoes"]
    assert snap["eligible"] is False


def test_btc_regime_can_veto_downtrend():
    up1=bars(drift=.001)
    up4=bars(drift=.002)
    assert bot.btc_regime(up1,up4)["ok"] is True
    down1=bars(drift=-.002)
    assert bot.btc_regime(down1,up4)["ok"] is False


def test_open_position_sizes_by_risk_and_never_exceeds_limits():
    state={
        "cash_usdt":20.08,"positions":{},"day_pnl":0.0,
    }
    snap={
        "symbol":"TESTUSDT","ask":1.0,"atr_pct_1h":0.02,"bar_time":1,
        "score":95,"groups":{"trend":25,"momentum":20,"flow":30,"volatility":10,"liquidity":10},
    }
    filters={"min_notional":1.0,"max_notional":1e9,"min_qty":0.0001,"max_qty":1e9,"step_size":0.0001}
    result=bot.open_position(state,snap,filters)
    assert result=="PAPER_OPENED"
    p=state["positions"]["TESTUSDT"]
    assert p["cost"] <= bot.CFG["risk"]["max_quote_per_trade_usdt"]*(1+bot.CFG["risk"]["fee_rate"])+0.01
    assert bot.stop_risk(p) <= bot.CFG["risk"]["max_risk_per_trade_usdt"]+1e-9
    assert 0 < p["stop"] < p["entry"] < p["target"]


def test_daily_loss_gate_blocks_new_position():
    state={"cash_usdt":20.08,"positions":{},"day_pnl":-2.0}
    snap={"symbol":"TESTUSDT","ask":1.0,"atr_pct_1h":0.02,"bar_time":1,"score":95,"groups":{}}
    filters={"min_notional":1.0,"max_notional":1e9,"min_qty":0.0001,"max_qty":1e9,"step_size":0.0001}
    assert bot.open_position(state,snap,filters)=="DAILY_LOSS_CAP"
