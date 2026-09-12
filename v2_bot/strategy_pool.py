from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean, median, pstdev
from typing import Any, Iterable

from .config import Settings
from .strategy import Candidate, _trend, ema


RESEARCH = "RESEARCH"
PAPER = "PAPER"
ACTIVE = "ACTIVE"
REJECTED = "REJECTED"


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    family: str
    status: str
    allowed_regimes: tuple[str, ...]
    take_profit_pct: float
    stop_loss_pct: float
    rationale: str


@dataclass(frozen=True)
class StrategyEvaluation:
    spec: StrategySpec
    regime: str
    score: int
    signal_ok: bool
    executable: bool
    failed_gates: tuple[str, ...]
    setup: str
    features: dict[str, float | bool]


# These four were explicitly rejected by the 180-day temporal OOS run. They are
# kept in the registry so a future refactor cannot silently reactivate them.
REJECTED_SPECS: tuple[StrategySpec, ...] = (
    StrategySpec("strict_current", "trend_retest", REJECTED, ("bull_trend", "recovery"), 0.0090, 0.0062, "180d temporal OOS PF 0.660"),
    StrategySpec("breakout_continuation", "breakout", REJECTED, ("bull_trend",), 0.0090, 0.0062, "180d temporal OOS PF 0.469"),
    StrategySpec("volatility_expansion", "volatility", REJECTED, ("bull_trend",), 0.0090, 0.0062, "180d temporal OOS PF 0.451"),
    StrategySpec("htf_pullback_reclaim", "trend_retest", REJECTED, ("bull_trend", "recovery"), 0.0090, 0.0062, "180d temporal OOS PF 0.540"),
)


# New fixed families. Status starts RESEARCH. A strategy is promoted to PAPER
# only after the frozen historical/OOS gate passes; Paper evidence is then
# collected independently before any future ACTIVE/Live consideration.
STRATEGY_SPECS: tuple[StrategySpec, ...] = (
    StrategySpec(
        "trend_momentum",
        "trend_momentum",
        RESEARCH,
        ("bull_trend", "recovery"),
        0.0120,
        0.0075,
        "HTF trend + short-horizon acceleration + flow confirmation",
    ),
    StrategySpec(
        "compression_breakout",
        "breakout",
        RESEARCH,
        ("bull_trend", "range"),
        0.0130,
        0.0065,
        "volatility compression followed by a non-extended volume breakout",
    ),
    StrategySpec(
        "mean_reversion_extreme",
        "mean_reversion",
        RESEARCH,
        ("range", "recovery"),
        0.0070,
        0.0055,
        "oversold statistical displacement with bullish reclaim",
    ),
    StrategySpec(
        "volume_anomaly_reversal",
        "volume_anomaly",
        RESEARCH,
        ("range", "recovery", "panic"),
        0.0080,
        0.0060,
        "high-volume downside impulse followed by flow-backed reclaim",
    ),
    StrategySpec(
        "btc_alt_leadlag",
        "lead_lag",
        RESEARCH,
        ("bull_trend", "recovery"),
        0.0100,
        0.0065,
        "BTC impulse with temporarily lagging altcoin catch-up",
    ),
    StrategySpec(
        "relative_strength_rotation",
        "cross_sectional_momentum",
        RESEARCH,
        ("bull_trend", "recovery"),
        0.0120,
        0.0070,
        "sustained 24h outperformance versus BTC with current flow support",
    ),
    StrategySpec(
        "crash_exhaustion_reversal",
        "exhaustion_reversal",
        RESEARCH,
        ("panic", "recovery"),
        0.0090,
        0.0070,
        "sharp multi-hour selloff with lower-wick exhaustion and buy-flow recovery",
    ),
    StrategySpec(
        "range_reversion",
        "mean_reversion",
        RESEARCH,
        ("range",),
        0.0065,
        0.0050,
        "range-regime statistical oversold bounce",
    ),
)


ALL_SPECS: tuple[StrategySpec, ...] = REJECTED_SPECS + STRATEGY_SPECS
SPEC_BY_ID = {spec.strategy_id: spec for spec in ALL_SPECS}


def _return(candles: list[dict[str, float]], bars: int) -> float:
    if len(candles) <= bars:
        return 0.0
    start = float(candles[-1 - bars]["close"])
    end = float(candles[-1]["close"])
    return (end / start - 1.0) if start > 0 else 0.0


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(len(closes) - period, len(closes))]
    gains = sum(max(delta, 0.0) for delta in deltas) / period
    losses = sum(max(-delta, 0.0) for delta in deltas) / period
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def _zscore(values: list[float], lookback: int = 20) -> float:
    sample = values[-lookback:]
    if len(sample) < lookback:
        return 0.0
    mean = fmean(sample)
    sigma = pstdev(sample)
    return (sample[-1] - mean) / sigma if sigma > 0 else 0.0


def _median_range(candles: Iterable[dict[str, float]]) -> float:
    values = [max(float(c["high"]) - float(c["low"]), 0.0) for c in candles]
    return median(values) if values else 0.0


def _classify_regime(btc_1h: list[dict[str, float]]) -> str:
    if len(btc_1h) < 55:
        return "unknown"
    closes = [float(c["close"]) for c in btc_1h]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    ret3 = _return(btc_1h, 3)
    ret12 = _return(btc_1h, 12)
    ranges = [
        (float(c["high"]) - float(c["low"])) / max(float(c["close"]), 1e-12)
        for c in btc_1h[-24:]
    ]
    vol = fmean(ranges) if ranges else 0.0

    if ret3 <= -0.025 or ret12 <= -0.06:
        return "panic"
    if ret3 >= 0.012 and closes[-1] > e20:
        return "recovery"
    if e20 > e50 and closes[-1] > e20:
        return "bull_trend"
    if e20 < e50 and closes[-1] < e20:
        return "bear_trend"
    if vol >= 0.018:
        return "high_vol"
    return "range"


def _features(
    *,
    candles_15m: list[dict[str, float]],
    candles_1h: list[dict[str, float]],
    candles_4h: list[dict[str, float]],
    btc_1h: list[dict[str, float]],
    spread_bps: float,
    quote_volume_24h: float,
    settings: Settings,
) -> dict[str, float | bool]:
    if min(len(candles_15m), len(candles_1h), len(candles_4h), len(btc_1h)) < 55:
        raise ValueError("Not enough closed candles for strategy pool")

    last = candles_15m[-1]
    previous = candles_15m[-2]
    prior20 = candles_15m[-21:-1]
    closes15 = [float(row["close"]) for row in candles_15m]
    closes1h = [float(row["close"]) for row in candles_1h]
    prior_qv = [float(row["quote_volume"]) for row in prior20]
    avg_qv = fmean(prior_qv) if prior_qv else 0.0
    relvol = float(last["quote_volume"]) / avg_qv if avg_qv > 0 else 0.0
    prev_relvol = float(previous["quote_volume"]) / avg_qv if avg_qv > 0 else 0.0
    taker = float(last["taker_buy_quote"]) / float(last["quote_volume"]) if float(last["quote_volume"]) > 0 else 0.0
    prior_high = max(float(row["high"]) for row in prior20)
    e20_15 = ema(closes15, 20)
    e20_1h = ema(closes1h, 20)
    e50_1h = ema(closes1h, 50)
    last_range = max(float(last["high"]) - float(last["low"]), 0.0)
    previous_range = max(float(previous["high"]) - float(previous["low"]), 0.0)
    med_range = _median_range(prior20)
    recent_compression = _median_range(candles_15m[-9:-1])
    baseline_compression = _median_range(candles_15m[-49:-9])
    lower_wick = min(float(last["open"]), float(last["close"])) - float(last["low"])
    close_location = (
        (float(last["close"]) - float(last["low"])) / last_range if last_range > 0 else 0.0
    )
    previous_body_return = (
        float(previous["close"]) / float(previous["open"]) - 1.0
        if float(previous["open"]) > 0
        else 0.0
    )
    previous_mid = (float(previous["high"]) + float(previous["low"])) / 2.0
    btc_ret3h = _return(btc_1h, 3)
    alt_ret3h = _return(candles_1h, 3)
    btc_ret24h = _return(btc_1h, 24)
    alt_ret24h = _return(candles_1h, 24)

    return {
        "price": float(last["close"]),
        "signal_open_time": float(last["open_time"]),
        "prior_high": prior_high,
        "liquidity": quote_volume_24h >= settings.min_quote_volume_24h,
        "spread": 0 <= spread_bps <= settings.max_spread_bps,
        "btc_trend": _trend(btc_1h),
        "trend_15m": _trend(candles_15m),
        "trend_1h": _trend(candles_1h),
        "trend_4h": _trend(candles_4h),
        "relvol": relvol,
        "prev_relvol": prev_relvol,
        "taker": taker,
        "bullish": float(last["close"]) > float(last["open"]),
        "close_above_ema20": float(last["close"]) > e20_15,
        "raw_breakout": float(last["close"]) > prior_high,
        "not_extended": float(last["close"]) <= prior_high * 1.008,
        "ret_1h": _return(candles_15m, 4),
        "ret_3h": alt_ret3h,
        "ret_4h": _return(candles_1h, 4),
        "ret_24h": alt_ret24h,
        "btc_ret_3h": btc_ret3h,
        "btc_ret_24h": btc_ret24h,
        "relative_strength_24h": alt_ret24h - btc_ret24h,
        "lag_gap_3h": btc_ret3h - alt_ret3h,
        "rsi14": _rsi(closes15),
        "z20": _zscore(closes15, 20),
        "range_ratio": last_range / med_range if med_range > 0 else 0.0,
        "previous_range_ratio": previous_range / med_range if med_range > 0 else 0.0,
        "compression_ratio": recent_compression / baseline_compression if baseline_compression > 0 else 1.0,
        "lower_wick_ratio": lower_wick / last_range if last_range > 0 else 0.0,
        "close_location": close_location,
        "previous_body_return": previous_body_return,
        "reclaimed_previous_mid": float(last["close"]) > previous_mid,
        "htf_not_broken": e20_1h >= e50_1h * 0.985,
    }


def _required_gates(spec: StrategySpec, regime: str, f: dict[str, float | bool]) -> dict[str, bool]:
    common = {
        "liquidity": bool(f["liquidity"]),
        "spread": bool(f["spread"]),
        "regime": regime in spec.allowed_regimes,
    }

    if spec.strategy_id == "trend_momentum":
        return {
            **common,
            "trend_1h": bool(f["trend_1h"]),
            "trend_4h": bool(f["trend_4h"]),
            "positive_1h_impulse": float(f["ret_1h"]) >= 0.004,
            "relative_volume": float(f["relvol"]) >= 1.25,
            "taker_flow": float(f["taker"]) >= 0.54,
            "bullish_close": bool(f["bullish"]),
            "above_ema20": bool(f["close_above_ema20"]),
        }

    if spec.strategy_id == "compression_breakout":
        return {
            **common,
            "compression": float(f["compression_ratio"]) <= 0.70,
            "breakout": bool(f["raw_breakout"]),
            "not_extended": bool(f["not_extended"]),
            "relative_volume": float(f["relvol"]) >= 1.70,
            "taker_flow": float(f["taker"]) >= 0.56,
            "bullish_close": bool(f["bullish"]),
        }

    if spec.strategy_id == "mean_reversion_extreme":
        return {
            **common,
            "zscore_extreme": float(f["z20"]) <= -1.80,
            "rsi_oversold": float(f["rsi14"]) <= 35.0,
            "htf_not_broken": bool(f["htf_not_broken"]),
            "taker_flow": float(f["taker"]) >= 0.54,
            "bullish_reclaim": bool(f["bullish"]),
        }

    if spec.strategy_id == "volume_anomaly_reversal":
        return {
            **common,
            "previous_volume_anomaly": float(f["prev_relvol"]) >= 2.0,
            "previous_down_impulse": float(f["previous_body_return"]) <= -0.008,
            "previous_range_expansion": float(f["previous_range_ratio"]) >= 1.5,
            "reclaim_previous_mid": bool(f["reclaimed_previous_mid"]),
            "taker_flow": float(f["taker"]) >= 0.56,
            "bullish_close": bool(f["bullish"]),
        }

    if spec.strategy_id == "btc_alt_leadlag":
        return {
            **common,
            "btc_impulse": float(f["btc_ret_3h"]) >= 0.010,
            "alt_lag": float(f["lag_gap_3h"]) >= 0.006,
            "alt_not_collapsing": float(f["ret_3h"]) >= -0.010,
            "trend_4h": bool(f["trend_4h"]),
            "taker_flow": float(f["taker"]) >= 0.55,
            "bullish_close": bool(f["bullish"]),
        }

    if spec.strategy_id == "relative_strength_rotation":
        return {
            **common,
            "relative_strength": float(f["relative_strength_24h"]) >= 0.020,
            "trend_1h": bool(f["trend_1h"]),
            "trend_4h": bool(f["trend_4h"]),
            "relative_volume": float(f["relvol"]) >= 1.20,
            "taker_flow": float(f["taker"]) >= 0.54,
            "bullish_close": bool(f["bullish"]),
        }

    if spec.strategy_id == "crash_exhaustion_reversal":
        return {
            **common,
            "four_hour_crash": float(f["ret_4h"]) <= -0.040,
            "rsi_extreme": float(f["rsi14"]) <= 30.0,
            "lower_wick_exhaustion": float(f["lower_wick_ratio"]) >= 0.45,
            "strong_close_location": float(f["close_location"]) >= 0.65,
            "taker_recovery": float(f["taker"]) >= 0.58,
        }

    if spec.strategy_id == "range_reversion":
        return {
            **common,
            "zscore_oversold": float(f["z20"]) <= -1.50,
            "rsi_soft_oversold": float(f["rsi14"]) <= 38.0,
            "relative_volume": float(f["relvol"]) >= 1.0,
            "taker_flow": float(f["taker"]) >= 0.54,
            "bullish_close": bool(f["bullish"]),
        }

    raise ValueError(f"Unsupported strategy: {spec.strategy_id}")


def evaluate_strategy(
    spec: StrategySpec,
    *,
    candles_15m: list[dict[str, float]],
    candles_1h: list[dict[str, float]],
    candles_4h: list[dict[str, float]],
    btc_1h: list[dict[str, float]],
    spread_bps: float,
    quote_volume_24h: float,
    settings: Settings,
    mode: str,
) -> StrategyEvaluation:
    regime = _classify_regime(btc_1h)
    f = _features(
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
        btc_1h=btc_1h,
        spread_bps=spread_bps,
        quote_volume_24h=quote_volume_24h,
        settings=settings,
    )
    gates = _required_gates(spec, regime, f)
    failed = [name for name, passed in gates.items() if not passed]
    signal_ok = not failed

    if mode == "shadow":
        status_allowed = spec.status in {RESEARCH, PAPER, ACTIVE}
    elif mode == "paper":
        status_allowed = spec.status == PAPER
    elif mode == "live":
        status_allowed = spec.status == ACTIVE
    else:
        status_allowed = False

    if signal_ok and not status_allowed:
        failed.append(f"strategy_status_{spec.status.lower()}_not_executable_in_{mode}")

    passed_count = sum(1 for value in gates.values() if value)
    score = round(100 * passed_count / max(len(gates), 1))
    return StrategyEvaluation(
        spec=spec,
        regime=regime,
        score=score,
        signal_ok=signal_ok,
        executable=signal_ok and status_allowed,
        failed_gates=tuple(failed),
        setup=spec.strategy_id,
        features=f,
    )


def evaluate_pool(
    *,
    symbol: str,
    candles_15m: list[dict[str, float]],
    candles_1h: list[dict[str, float]],
    candles_4h: list[dict[str, float]],
    btc_1h: list[dict[str, float]],
    spread_bps: float,
    quote_volume_24h: float,
    settings: Settings,
    mode: str,
    specs: tuple[StrategySpec, ...] = STRATEGY_SPECS,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for spec in specs:
        evaluation = evaluate_strategy(
            spec,
            candles_15m=candles_15m,
            candles_1h=candles_1h,
            candles_4h=candles_4h,
            btc_1h=btc_1h,
            spread_bps=spread_bps,
            quote_volume_24h=quote_volume_24h,
            settings=settings,
            mode=mode,
        )
        f = evaluation.features
        candidate = Candidate(
            symbol=symbol,
            score=evaluation.score,
            price=float(f["price"]),
            signal_open_time=float(f["signal_open_time"]),
            previous_20_high=float(f["prior_high"]),
            relative_volume=float(f["relvol"]),
            taker_buy_ratio=float(f["taker"]),
            spread_bps=spread_bps,
            quote_volume_24h=quote_volume_24h,
            btc_regime_ok=bool(f["btc_trend"]),
            trend_15m=bool(f["trend_15m"]),
            trend_1h=bool(f["trend_1h"]),
            trend_4h=bool(f["trend_4h"]),
            breakout=bool(f["raw_breakout"]),
            rel_volume_ok=float(f["relvol"]) >= 1.5,
            taker_flow_ok=float(f["taker"]) >= 0.56,
            eligible=evaluation.executable,
            pullback=False,
            entry_setup=evaluation.setup,
            breakout_retest=False,
            breakout_level=float(f["prior_high"]),
            strategy_id=spec.strategy_id,
            strategy_family=spec.family,
            strategy_status=spec.status,
            market_regime=evaluation.regime,
            strategy_signal_ok=evaluation.signal_ok,
            strategy_failed_gates=evaluation.failed_gates,
            take_profit_pct=spec.take_profit_pct,
            stop_loss_pct=spec.stop_loss_pct,
        )
        candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            1 if item.eligible else 0,
            1 if item.strategy_signal_ok else 0,
            item.score,
            item.relative_volume,
        ),
        reverse=True,
    )
    return candidates


def registry_snapshot() -> list[dict[str, Any]]:
    return [
        {
            "strategy_id": spec.strategy_id,
            "family": spec.family,
            "status": spec.status,
            "allowed_regimes": list(spec.allowed_regimes),
            "take_profit_pct": spec.take_profit_pct,
            "stop_loss_pct": spec.stop_loss_pct,
            "rationale": spec.rationale,
        }
        for spec in ALL_SPECS
    ]
