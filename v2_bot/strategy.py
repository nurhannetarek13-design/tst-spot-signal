from __future__ import annotations

from dataclasses import dataclass, asdict
from statistics import fmean
from typing import Any


@dataclass(frozen=True)
class Candidate:
    symbol: str
    score: int
    price: float
    signal_open_time: float
    previous_20_high: float
    relative_volume: float
    taker_buy_ratio: float
    spread_bps: float
    quote_volume_24h: float
    btc_regime_ok: bool
    trend_15m: bool
    trend_1h: bool
    trend_4h: bool
    breakout: bool
    rel_volume_ok: bool
    taker_flow_ok: bool
    eligible: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"Need at least {period} values")
    seed = fmean(values[:period])
    alpha = 2.0 / (period + 1.0)
    out = seed
    for value in values[period:]:
        out = alpha * value + (1.0 - alpha) * out
    return out


def _trend(candles: list[dict[str, float]]) -> bool:
    closes = [c["close"] for c in candles]
    if len(closes) < 50:
        return False
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    return ema20 > ema50 and closes[-1] > ema20


def evaluate_candidate(
    *,
    symbol: str,
    candles_15m: list[dict[str, float]],
    candles_1h: list[dict[str, float]],
    candles_4h: list[dict[str, float]],
    btc_1h: list[dict[str, float]],
    spread_bps: float,
    quote_volume_24h: float,
    min_quote_volume_24h: float,
    max_spread_bps: float,
    min_score: int,
) -> Candidate:
    if len(candles_15m) < 55 or len(candles_1h) < 55 or len(candles_4h) < 55 or len(btc_1h) < 55:
        raise ValueError("Not enough closed candles")

    last = candles_15m[-1]
    previous_20 = candles_15m[-21:-1]
    previous_20_high = max(c["high"] for c in previous_20)
    price = last["close"]
    signal_open_time = last["open_time"]

    prior_quote_volumes = [c["quote_volume"] for c in previous_20]
    avg_prior_quote_volume = fmean(prior_quote_volumes) if prior_quote_volumes else 0.0
    relative_volume = last["quote_volume"] / avg_prior_quote_volume if avg_prior_quote_volume > 0 else 0.0
    taker_buy_ratio = last["taker_buy_quote"] / last["quote_volume"] if last["quote_volume"] > 0 else 0.0

    liquidity_ok = quote_volume_24h >= min_quote_volume_24h
    spread_ok = 0 <= spread_bps <= max_spread_bps
    btc_regime_ok = _trend(btc_1h)
    trend_15m = _trend(candles_15m)
    trend_1h = _trend(candles_1h)
    trend_4h = _trend(candles_4h)
    breakout = price > previous_20_high
    rel_volume_ok = relative_volume >= 1.5
    taker_flow_ok = taker_buy_ratio >= 0.56

    score = 0
    score += 10 if liquidity_ok else 0
    score += 10 if spread_ok else 0
    score += 10 if btc_regime_ok else 0
    score += 10 if trend_15m else 0
    score += 10 if trend_1h else 0
    score += 10 if trend_4h else 0
    score += 15 if breakout else 0
    score += 15 if rel_volume_ok else 0
    score += 10 if taker_flow_ok else 0

    # Score is used for ranking/diagnostics. Eligibility is stricter: every
    # entry gate must pass. This prevents a 90/100 candidate from becoming
    # tradable while one critical trend/flow/breakout condition is missing.
    all_entry_gates_ok = all(
        (
            liquidity_ok,
            spread_ok,
            btc_regime_ok,
            trend_15m,
            trend_1h,
            trend_4h,
            breakout,
            rel_volume_ok,
            taker_flow_ok,
        )
    )
    eligible = all_entry_gates_ok and score >= min_score

    return Candidate(
        symbol=symbol,
        score=score,
        price=price,
        signal_open_time=signal_open_time,
        previous_20_high=previous_20_high,
        relative_volume=relative_volume,
        taker_buy_ratio=taker_buy_ratio,
        spread_bps=spread_bps,
        quote_volume_24h=quote_volume_24h,
        btc_regime_ok=btc_regime_ok,
        trend_15m=trend_15m,
        trend_1h=trend_1h,
        trend_4h=trend_4h,
        breakout=breakout,
        rel_volume_ok=rel_volume_ok,
        taker_flow_ok=taker_flow_ok,
        eligible=eligible,
    )
