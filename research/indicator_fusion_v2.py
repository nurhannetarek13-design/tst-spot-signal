"""Indicator Fusion V2: event-driven, regime-conditioned, research-only.

V2 is intentionally structurally different from V1:
- no continuous re-entry from a high score alone
- requires a first-breakout event
- requires a variance-ratio trend regime
- keeps flow/liquidity as independent confirmation
- never authorizes live trading
"""

from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd

from research.indicator_fusion_v1 import _atr_pct, _ema, _rsi, _safe_z, _weighted_bool


DEFAULT_PARAMS_V2 = {
    "scoreMin": 72.0,
    "familiesMin": 4,
    "emaFast": 24,
    "emaSlow": 72,
    "emaTrend": 200,
    "rocLookback": 6,
    "breakoutLookback": 24,
    "relvolLookback": 24,
    "flowLookback": 168,
    "atrLookback": 14,
    "atrRankLookback": 168,
    "varianceRatioWindow": 96,
    "varianceRatioQ": 4,
    "varianceRatioMin": 1.02,
    "minRollingQuoteVolume24h": 20_000_000.0,
    "takerFloor": 0.50,
    "takerStrong": 0.55,
    "rsiLow": 50.0,
    "rsiHigh": 72.0,
    "rsiVeto": 78.0,
    "atrMin": 0.005,
    "atrMax": 0.05,
    "atrVeto": 0.07,
    "relvolStrong": 1.20,
    "relvolFloor": 0.80,
    "rocStrong": 0.005,
    "holdBars": 24,
    "riskAtrMult": 1.5,
    "riskMinPct": 0.012,
    "riskMaxPct": 0.030,
    "rewardRisk": 2.0,
}

FAMILY_WEIGHTS_V2 = {
    "price_state": 30.0,
    "flow": 30.0,
    "momentum": 15.0,
    "regime_vol": 15.0,
    "liquidity": 10.0,
}

FAMILY_CONFIRM_MIN_V2 = {
    "price_state": 18.0,
    "flow": 18.0,
    "momentum": 8.0,
    "regime_vol": 8.0,
    "liquidity": 6.0,
}


def variance_ratio_series(close: pd.Series, window: int = 96, q: int = 4) -> pd.Series:
    if q < 2 or window < q * 8:
        raise ValueError("invalid variance-ratio parameters")
    lr = np.log(close / close.shift(1))
    one = lr.rolling(window).var(ddof=0)
    agg = lr.rolling(q).sum()
    qvar = agg.rolling(window).var(ddof=0)
    return qvar / (q * one.replace(0, np.nan))


def compute_indicator_fusion_v2(
    df: pd.DataFrame,
    params: Mapping[str, float | int] | None = None,
) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS_V2, **dict(params or {})}
    required = {"open", "high", "low", "close", "volume", "quote_volume", "taker_quote"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")

    out = pd.DataFrame(index=df.index)
    c = pd.to_numeric(df["close"], errors="coerce")
    qv = pd.to_numeric(df["quote_volume"], errors="coerce")
    tq = pd.to_numeric(df["taker_quote"], errors="coerce")

    ef = _ema(c, int(p["emaFast"]))
    es = _ema(c, int(p["emaSlow"]))
    et = _ema(c, int(p["emaTrend"]))
    rsi = _rsi(c)
    roc = c.pct_change(int(p["rocLookback"]))
    atr = _atr_pct(df, int(p["atrLookback"]))
    atr_rank = atr.rolling(int(p["atrRankLookback"])).rank(pct=True)

    relvol = qv / qv.rolling(int(p["relvolLookback"])).median().replace(0, np.nan)
    taker_ratio = tq / qv.replace(0, np.nan)
    taker_z = _safe_z(taker_ratio, int(p["flowLookback"]))
    qv24 = qv.rolling(24).sum()

    lookback = int(p["breakoutLookback"])
    prior_high = df["high"].rolling(lookback).max().shift(1)
    breakout_cross = (c > prior_high) & (c.shift(1) <= prior_high.shift(1))

    vr = variance_ratio_series(
        c,
        int(p["varianceRatioWindow"]),
        int(p["varianceRatioQ"]),
    )
    regime_ok = (vr >= float(p["varianceRatioMin"])) & (c > et)

    price_state = (
        _weighted_bool(c > ef, 8)
        + _weighted_bool(ef > es, 8)
        + _weighted_bool(ef > ef.shift(3), 6)
        + _weighted_bool(c > et, 8)
    )
    flow = (
        _weighted_bool(taker_ratio >= float(p["takerStrong"]), 12)
        + _weighted_bool(taker_z >= 0.5, 10)
        + _weighted_bool(relvol >= float(p["relvolStrong"]), 8)
    )
    momentum = (
        _weighted_bool(roc >= float(p["rocStrong"]), 7)
        + _weighted_bool(rsi.between(float(p["rsiLow"]), float(p["rsiHigh"])), 4)
        + _weighted_bool(rsi > rsi.shift(3), 4)
    )
    regime_vol = (
        _weighted_bool(vr >= float(p["varianceRatioMin"]), 8)
        + _weighted_bool(atr.between(float(p["atrMin"]), float(p["atrMax"])), 4)
        + _weighted_bool(atr_rank.between(0.20, 0.80), 3)
    )
    liquidity = (
        _weighted_bool(qv24 >= float(p["minRollingQuoteVolume24h"]), 6)
        + _weighted_bool(relvol >= float(p["relvolFloor"]), 4)
    )

    families = {
        "price_state": price_state,
        "flow": flow,
        "momentum": momentum,
        "regime_vol": regime_vol,
        "liquidity": liquidity,
    }
    for name, series in families.items():
        out[f"{name}_score"] = series.clip(lower=0, upper=FAMILY_WEIGHTS_V2[name])

    out["score"] = sum(out[f"{name}_score"] for name in FAMILY_WEIGHTS_V2)
    confirms = [
        out[f"{name}_score"] >= FAMILY_CONFIRM_MIN_V2[name]
        for name in FAMILY_WEIGHTS_V2
    ]
    out["families"] = sum(x.astype(int) for x in confirms)

    warmup_missing = (
        qv24.isna()
        | et.isna()
        | taker_z.isna()
        | atr_rank.isna()
        | prior_high.isna()
        | vr.isna()
    )
    hard_veto = (
        warmup_missing
        | (rsi > float(p["rsiVeto"]))
        | (taker_ratio < float(p["takerFloor"]))
        | (atr > float(p["atrVeto"]))
        | (qv24 < float(p["minRollingQuoteVolume24h"]))
        | (qv <= 0)
    )

    out["event"] = breakout_cross.fillna(False)
    out["regime_ok"] = regime_ok.fillna(False)
    out["hard_veto"] = hard_veto.fillna(True)
    out["enter"] = (
        out["event"]
        & out["regime_ok"]
        & (out["score"] >= float(p["scoreMin"]))
        & (out["families"] >= int(p["familiesMin"]))
        & ~out["hard_veto"]
    ).fillna(False)

    out["rsi"] = rsi
    out["roc"] = roc
    out["atr_pct"] = atr
    out["atr_rank"] = atr_rank
    out["relvol"] = relvol
    out["taker_ratio"] = taker_ratio
    out["taker_z"] = taker_z
    out["rolling_quote_volume_24h"] = qv24
    out["variance_ratio"] = vr
    out["prior_high"] = prior_high
    return out


def risk_geometry_v2(
    atr_pct: float,
    params: Mapping[str, float | int] | None = None,
) -> tuple[float, float, int]:
    p = {**DEFAULT_PARAMS_V2, **dict(params or {})}
    risk = float(np.clip(
        float(atr_pct) * float(p["riskAtrMult"]),
        float(p["riskMinPct"]),
        float(p["riskMaxPct"]),
    ))
    return risk, risk * float(p["rewardRisk"]), int(p["holdBars"])


def contract_v2() -> dict:
    return {
        "family": "INDICATOR_FUSION_V2_EVENT_REGIME",
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "automaticPromotion": False,
        "parameterSearch": False,
        "entryEvent": "FIRST_BREAKOUT_CROSS",
        "regime": "ROLLING_VARIANCE_RATIO_PLUS_EMA200",
        "weights": dict(FAMILY_WEIGHTS_V2),
        "confirmMinimums": dict(FAMILY_CONFIRM_MIN_V2),
        "params": dict(DEFAULT_PARAMS_V2),
    }
