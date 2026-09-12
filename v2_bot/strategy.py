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
    pullback: bool = False
    entry_setup: str = "none"
    breakout_retest: bool = False
    breakout_level: float = 0.0
    # Runtime V2 may route several independent strategy families. These
    # defaults preserve backwards compatibility with the original evaluator
    # and its unit tests while allowing the production runtime to report the
    # actual strategy selected by the router.
    strategy_id: str = "strict_current"
    strategy_family: str = "trend_retest"
    strategy_status: str = "LEGACY"
    market_regime: str = "unknown"
    strategy_signal_ok: bool = False
    strategy_failed_gates: tuple[str, ...] = ()
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None

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


def _confirmed_pullback(candles: list[dict[str, float]]) -> bool:
    """Require a two-candle EMA20 pullback/reclaim confirmation."""
    if len(candles) < 50:
        return False
    closes = [c["close"] for c in candles]
    ema20 = ema(closes, 20)
    previous = candles[-2]
    last = candles[-1]

    touched_ema_zone = previous["low"] <= ema20 * 1.0025
    held_structure = previous["close"] >= ema20 * 0.995
    bullish_confirmation = (
        last["close"] > previous["high"]
        and last["close"] > last["open"]
        and last["close"] > ema20
    )
    return touched_ema_zone and held_structure and bullish_confirmation


def _confirmed_breakout_retest(candles: list[dict[str, float]]) -> tuple[bool, float]:
    """Require a closed breakout candle followed by a closed retest/hold.

    The penultimate candle must close above the highest high of the 20 candles
    before it. The newest candle must trade back into that breakout level,
    close bullish on/above it, and not finish more than 1% above the level.
    This prevents a raw or already-extended breakout from becoming an entry.
    """
    if len(candles) < 23:
        return False, 0.0

    history = candles[-22:-2]
    breakout_candle = candles[-2]
    last = candles[-1]
    breakout_level = max(c["high"] for c in history)

    breakout_confirmed = breakout_candle["close"] > breakout_level
    retest_touched = last["low"] <= breakout_level * 1.0025
    retest_held = last["close"] >= breakout_level
    bullish_hold = last["close"] > last["open"]
    not_extended = last["close"] <= breakout_level * 1.01

    confirmed = all(
        (
            breakout_confirmed,
            retest_touched,
            retest_held,
            bullish_hold,
            not_extended,
        )
    )
    return confirmed, breakout_level


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

    # Raw breakout is kept for diagnostics/pre-alerts only. It is not an entry.
    breakout = price > previous_20_high
    breakout_retest, breakout_level = _confirmed_breakout_retest(candles_15m)
    pullback = _confirmed_pullback(candles_15m)
    entry_setup_ok = breakout_retest or pullback
    entry_setup = "breakout_retest" if breakout_retest else "pullback" if pullback else "none"

    rel_volume_ok = relative_volume >= 1.5
    taker_flow_ok = taker_buy_ratio >= 0.56

    score = 0
    score += 10 if liquidity_ok else 0
    score += 10 if spread_ok else 0
    score += 10 if btc_regime_ok else 0
    score += 10 if trend_15m else 0
    score += 10 if trend_1h else 0
    score += 10 if trend_4h else 0
    score += 15 if entry_setup_ok else 0
    score += 15 if rel_volume_ok else 0
    score += 10 if taker_flow_ok else 0

    # Score ranks candidates, but every mandatory gate must pass. A raw
    # breakout alone is deliberately insufficient; it needs a retest/hold.
    all_entry_gates_ok = all(
        (
            liquidity_ok,
            spread_ok,
            btc_regime_ok,
            trend_15m,
            trend_1h,
            trend_4h,
            entry_setup_ok,
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
        pullback=pullback,
        entry_setup=entry_setup,
        breakout_retest=breakout_retest,
        breakout_level=breakout_level,
        strategy_signal_ok=eligible,
    )
