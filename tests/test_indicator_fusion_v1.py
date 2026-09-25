import numpy as np
import pandas as pd

from research.indicator_fusion_v1 import (
    DEFAULT_PARAMS,
    FAMILY_WEIGHTS,
    compute_indicator_fusion,
    contract,
    risk_geometry,
)


def sample_frame(n=260, weak_last_flow=False):
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    rets = np.asarray(([0.009, -0.005] * (n // 2 + 1))[:n], dtype=float)
    close = 1.0 * np.cumprod(1.0 + rets)
    open_ = np.r_[close[0] / (1 + rets[0]), close[:-1]]
    high = np.maximum(open_, close) * 1.002
    low = np.minimum(open_, close) * 0.998
    qv = np.full(n, 2_000_000.0)
    qv[::4] = 2_500_000.0
    volume = qv / close
    ratio = np.asarray(([0.52, 0.58, 0.54, 0.61] * (n // 4 + 1))[:n], dtype=float)
    if weak_last_flow:
        ratio[-1] = 0.40
    taker_quote = qv * ratio
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "quote_volume": qv,
            "taker_quote": taker_quote,
        },
        index=idx,
    )


def test_contract_is_research_only_and_weights_sum_to_100():
    c = contract()
    assert c["family"] == "INDICATOR_FUSION_V1"
    assert c["authorization"] == "RESEARCH_ONLY"
    assert c["liveTrading"] is False
    assert c["automaticPromotion"] is False
    assert sum(FAMILY_WEIGHTS.values()) == 100.0


def test_fusion_can_emit_signal_from_multiple_independent_families():
    df = sample_frame()
    params = {
        **DEFAULT_PARAMS,
        "scoreMin": 60,
        "familiesMin": 4,
        "emaTrend": 50,
        "flowLookback": 24,
        "atrRankLookback": 24,
        "breakoutLookback": 12,
        "rocStrong": 0.0,
        "relvolStrong": 1.0,
        "minRollingQuoteVolume24h": 1_000_000,
    }
    out = compute_indicator_fusion(df, params)
    assert out["score"].max() <= 100.0
    assert out["families"].max() <= 6
    assert bool(out["enter"].iloc[-80:].any())


def test_weak_aggressive_buy_flow_is_hard_veto():
    df = sample_frame(weak_last_flow=True)
    params = {
        **DEFAULT_PARAMS,
        "emaTrend": 50,
        "flowLookback": 24,
        "atrRankLookback": 24,
        "breakoutLookback": 12,
        "minRollingQuoteVolume24h": 1_000_000,
    }
    out = compute_indicator_fusion(df, params)
    assert bool(out["hard_veto"].iloc[-1])
    assert not bool(out["enter"].iloc[-1])


def test_risk_geometry_is_bounded_and_positive():
    low = risk_geometry(0.001)
    mid = risk_geometry(0.015)
    high = risk_geometry(0.10)
    assert low[0] == DEFAULT_PARAMS["riskMinPct"]
    assert high[0] == DEFAULT_PARAMS["riskMaxPct"]
    assert low[1] > low[0]
    assert mid[1] > mid[0]
    assert high[1] > high[0]
    assert low[2] == DEFAULT_PARAMS["holdBars"]
