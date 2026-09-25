"""Deterministic candle-derived Indicator Fusion V1.

This module is research-only. It produces orthogonal-ish feature-family scores
(trend, momentum, flow, volatility, structure, liquidity) and an entry mask.
It deliberately does not authorize live trading and does not auto-tune weights.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd


DEFAULT_PARAMS = {
    "scoreMin": 70.0,
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
    "minRollingQuoteVolume24h": 20_000_000.0,
    "takerFloor": 0.48,
    "takerStrong": 0.55,
    "rsiLow": 52.0,
    "rsiHigh": 72.0,
    "rsiVeto": 82.0,
    "atrMin": 0.005,
    "atrMax": 0.05,
    "atrVeto": 0.08,
    "relvolStrong": 1.20,
    "relvolFloor": 0.80,
    "rocStrong": 0.01,
    "holdBars": 18,
    "riskAtrMult": 1.5,
    "riskMinPct": 0.012,
    "riskMaxPct": 0.035,
    "rewardRisk": 1.8,
}

FAMILY_WEIGHTS = {
    "trend": 20.0,
    "momentum": 15.0,
    "flow": 25.0,
    "volatility": 10.0,
    "structure": 20.0,
    "liquidity": 10.0,
}

FAMILY_CONFIRM_MIN = {
    "trend": 12.0,
    "momentum": 10.0,
    "flow": 15.0,
    "volatility": 5.0,
    "structure": 10.0,
    "liquidity": 5.0,
}


@dataclass(frozen=True)
class FusionSnapshot:
    score: float
    families: int
    hard_veto: bool
    enter: bool


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=int(n), adjust=False).mean()


def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    gains = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    losses = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gains / losses.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def _atr_pct(df: pd.DataFrame, n: int) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat(
        [
            (df["high"] - df["low"]).abs(),
            (df["high"] - prev).abs(),
            (df["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(int(n)).mean() / df["close"].replace(0, np.nan)


def _safe_z(s: pd.Series, n: int) -> pd.Series:
    mu = s.rolling(int(n)).mean()
    sd = s.rolling(int(n)).std(ddof=0).replace(0, np.nan)
    return ((s - mu) / sd).replace([np.inf, -np.inf], np.nan)


def _weighted_bool(cond: pd.Series, weight: float) -> pd.Series:
    return cond.fillna(False).astype(float) * float(weight)


def compute_indicator_fusion(
    df: pd.DataFrame,
    params: Mapping[str, float | int] | None = None,
) -> pd.DataFrame:
    """Return feature-family scores plus deterministic Indicator Fusion entry mask.

    Required columns:
      open, high, low, close, volume, quote_volume, taker_quote

    taker_quote / quote_volume is used as the candle-level aggressive-buy proxy.
    L2/order-book confirmation is intentionally excluded from historical scoring.
    """
    p = {**DEFAULT_PARAMS, **dict(params or {})}
    required = {"open", "high", "low", "close", "volume", "quote_volume", "taker_quote"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")

    out = pd.DataFrame(index=df.index)
    c = pd.to_numeric(df["close"], errors="coerce")
    qv = pd.to_numeric(df["quote_volume"], errors="coerce")
    tq = pd.to_numeric(df["taker_quote"], errors="coerce")

    ema_fast = _ema(c, int(p["emaFast"]))
    ema_slow = _ema(c, int(p["emaSlow"]))
    ema_trend = _ema(c, int(p["emaTrend"]))
    rsi = _rsi(c)
    roc = c.pct_change(int(p["rocLookback"]))
    atr = _atr_pct(df, int(p["atrLookback"]))

    relvol_med = qv.rolling(int(p["relvolLookback"])).median().replace(0, np.nan)
    relvol = qv / relvol_med
    taker_ratio = tq / qv.replace(0, np.nan)
    taker_z = _safe_z(taker_ratio, int(p["flowLookback"]))
    atr_rank = atr.rolling(int(p["atrRankLookback"])).rank(pct=True)
    qv24 = qv.rolling(24).sum()
    prior_high = df["high"].rolling(int(p["breakoutLookback"])).max().shift(1)
    prior_mid = (
        df["high"].rolling(int(p["breakoutLookback"])).max().shift(1)
        + df["low"].rolling(int(p["breakoutLookback"])).min().shift(1)
    ) / 2.0
    higher_low = df["low"].rolling(6).min() > df["low"].rolling(12).min().shift(6)

    trend = (
        _weighted_bool(c > ema_fast, 6)
        + _weighted_bool(ema_fast > ema_slow, 6)
        + _weighted_bool(ema_fast > ema_fast.shift(3), 4)
        + _weighted_bool(c > ema_trend, 4)
    )
    momentum = (
        _weighted_bool(roc >= float(p["rocStrong"]), 5)
        + _weighted_bool(rsi.between(float(p["rsiLow"]), float(p["rsiHigh"])), 5)
        + _weighted_bool(rsi > rsi.shift(3), 5)
    )
    flow = (
        _weighted_bool(relvol >= float(p["relvolStrong"]), 8)
        + _weighted_bool(taker_ratio >= float(p["takerStrong"]), 10)
        + _weighted_bool(taker_z >= 0.5, 7)
    )
    volatility = (
        _weighted_bool(atr.between(float(p["atrMin"]), float(p["atrMax"])), 5)
        + _weighted_bool(atr_rank.between(0.25, 0.85), 5)
    )
    structure = (
        _weighted_bool(c > prior_high, 10)
        + _weighted_bool(c > prior_mid, 5)
        + _weighted_bool(higher_low, 5)
    )
    liquidity = (
        _weighted_bool(relvol >= float(p["relvolFloor"]), 5)
        + _weighted_bool(qv24 >= float(p["minRollingQuoteVolume24h"]), 5)
    )

    family_scores = {
        "trend": trend,
        "momentum": momentum,
        "flow": flow,
        "volatility": volatility,
        "structure": structure,
        "liquidity": liquidity,
    }
    for name, series in family_scores.items():
        out[f"{name}_score"] = series.clip(lower=0, upper=FAMILY_WEIGHTS[name])

    out["score"] = sum(out[f"{name}_score"] for name in FAMILY_WEIGHTS)
    confirmations = [
        out[f"{name}_score"] >= FAMILY_CONFIRM_MIN[name]
        for name in FAMILY_WEIGHTS
    ]
    out["families"] = sum(x.astype(int) for x in confirmations)

    hard_veto = (
        qv24.isna()
        | ema_trend.isna()
        | taker_z.isna()
        | atr_rank.isna()
        | prior_high.isna()
        | (rsi > float(p["rsiVeto"]))
        | (taker_ratio < float(p["takerFloor"]))
        | (atr > float(p["atrVeto"]))
        | (qv24 < float(p["minRollingQuoteVolume24h"]))
        | (qv <= 0)
    )
    out["hard_veto"] = hard_veto.fillna(True)
    out["enter"] = (
        (out["score"] >= float(p["scoreMin"]))
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
    out["prior_high"] = prior_high
    return out


def latest_snapshot(
    df: pd.DataFrame,
    params: Mapping[str, float | int] | None = None,
) -> FusionSnapshot:
    x = compute_indicator_fusion(df, params)
    if x.empty:
        return FusionSnapshot(0.0, 0, True, False)
    row = x.iloc[-1]
    return FusionSnapshot(
        score=float(row["score"]),
        families=int(row["families"]),
        hard_veto=bool(row["hard_veto"]),
        enter=bool(row["enter"]),
    )


def risk_geometry(
    atr_pct: float,
    params: Mapping[str, float | int] | None = None,
) -> tuple[float, float, int]:
    """Return stop %, target %, hold bars from the independent risk layer."""
    p = {**DEFAULT_PARAMS, **dict(params or {})}
    risk = float(np.clip(
        float(atr_pct) * float(p["riskAtrMult"]),
        float(p["riskMinPct"]),
        float(p["riskMaxPct"]),
    ))
    target = risk * float(p["rewardRisk"])
    return risk, target, int(p["holdBars"])


def contract() -> dict:
    return {
        "family": "INDICATOR_FUSION_V1",
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "automaticPromotion": False,
        "weights": dict(FAMILY_WEIGHTS),
        "confirmMinimums": dict(FAMILY_CONFIRM_MIN),
        "params": dict(DEFAULT_PARAMS),
    }
