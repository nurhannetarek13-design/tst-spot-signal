from __future__ import annotations

from statistics import fmean, median, pstdev

from .config import Settings
from .strategy import Candidate, _trend, ema


STRATEGY_ID = "liquidity_reversal_1h"
STRATEGY_FAMILY = "liquidity_reversal"
STRATEGY_STATUS = "PAPER_EXPERIMENT"
TAKE_PROFIT_PCT = 0.05
STOP_LOSS_PCT = 0.03
MAX_HOLD_HOURS = 12.0
MIN_QUOTE_VOLUME = 20_000_000.0
MAX_QUOTE_VOLUME = 150_000_000.0
MAX_PRICE = 3.0
Z_LOOKBACK = 720
RET_LOOKBACK = 6
VOLUME_LOOKBACK = 168
Z_MAX = -2.0
RSI_MAX = 35.0
VOLUME_RATIO_MAX = 1.10

MAJORS = {"BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "TRX", "LTC", "BCH", "LINK", "AVAX", "DOT"}
EXCLUDED = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "AEUR", "TRY", "BRL", "GBP", "AUD", "USD1", "RLUSD", "USDE", "PAXG", "XAUT"}


def allowed_base(base: str) -> bool:
    return bool(
        base
        and base.isascii()
        and base.isalnum()
        and base not in MAJORS
        and base not in EXCLUDED
        and not base.endswith(("UP", "DOWN", "BULL", "BEAR"))
    )


def universe_eligible(*, base_asset: str, price: float, quote_volume_24h: float) -> bool:
    return (
        allowed_base(base_asset)
        and 0 < price <= MAX_PRICE
        and MIN_QUOTE_VOLUME <= quote_volume_24h <= MAX_QUOTE_VOLUME
    )


def _wilder_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(delta, 0.0) for delta in deltas]
    losses = [max(-delta, 0.0) for delta in deltas]
    avg_gain = fmean(gains[:period])
    avg_loss = fmean(losses[:period])
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((period - 1) * avg_gain + gain) / period
        avg_loss = ((period - 1) * avg_loss + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def evaluate_liquidity_reversal(
    *,
    symbol: str,
    base_asset: str,
    candles_1h: list[dict[str, float]],
    candles_15m: list[dict[str, float]],
    candles_4h: list[dict[str, float]],
    btc_1h: list[dict[str, float]],
    spread_bps: float,
    quote_volume_24h: float,
    settings: Settings,
    mode: str,
) -> Candidate:
    if len(candles_1h) < Z_LOOKBACK + RET_LOOKBACK + 2:
        raise ValueError("Need at least 728 closed 1h candles for liquidity reversal")

    closes = [float(row["close"]) for row in candles_1h]
    last = candles_1h[-1]
    price = float(last["close"])
    return6 = [
        closes[index] / closes[index - RET_LOOKBACK] - 1.0
        for index in range(RET_LOOKBACK, len(closes))
        if closes[index - RET_LOOKBACK] > 0
    ]
    z_sample = return6[-Z_LOOKBACK:]
    z_mean = fmean(z_sample)
    z_sigma = pstdev(z_sample)
    z_score = (z_sample[-1] - z_mean) / z_sigma if z_sigma > 0 else 0.0

    quote_volumes = [float(row["quote_volume"]) for row in candles_1h]
    volume_median = median(quote_volumes[-VOLUME_LOOKBACK:])
    volume_ratio = quote_volumes[-1] / volume_median if volume_median > 0 else float("inf")
    rsi14 = _wilder_rsi(closes, 14)
    ema24 = ema(closes, 24)

    gates = {
        "universe": universe_eligible(
            base_asset=base_asset,
            price=price,
            quote_volume_24h=quote_volume_24h,
        ),
        "spread": 0 <= spread_bps <= settings.max_spread_bps,
        "zscore": z_score <= Z_MAX,
        "volume_ratio": volume_ratio <= VOLUME_RATIO_MAX,
        "rsi": rsi14 <= RSI_MAX,
        "below_ema24": price < ema24,
    }
    failed = tuple(name for name, passed in gates.items() if not passed)
    signal_ok = not failed
    executable = signal_ok and mode == "paper"
    score = round(100 * sum(1 for passed in gates.values() if passed) / len(gates))

    previous_20 = candles_15m[-21:-1] if len(candles_15m) >= 21 else candles_15m[:-1]
    previous_high = max((float(row["high"]) for row in previous_20), default=price)
    last15 = candles_15m[-1] if candles_15m else last
    prior_qv = [float(row["quote_volume"]) for row in previous_20]
    avg_qv = fmean(prior_qv) if prior_qv else 0.0
    relvol = float(last15["quote_volume"]) / avg_qv if avg_qv > 0 else 0.0
    taker = (
        float(last15["taker_buy_quote"]) / float(last15["quote_volume"])
        if float(last15["quote_volume"]) > 0
        else 0.0
    )

    return Candidate(
        symbol=symbol,
        score=score,
        price=price,
        signal_open_time=float(last["open_time"]),
        previous_20_high=previous_high,
        relative_volume=relvol,
        taker_buy_ratio=taker,
        spread_bps=spread_bps,
        quote_volume_24h=quote_volume_24h,
        btc_regime_ok=_trend(btc_1h),
        trend_15m=_trend(candles_15m) if len(candles_15m) >= 50 else False,
        trend_1h=_trend(candles_1h),
        trend_4h=_trend(candles_4h),
        breakout=False,
        rel_volume_ok=True,
        taker_flow_ok=True,
        eligible=executable,
        pullback=False,
        entry_setup=STRATEGY_ID,
        breakout_retest=False,
        breakout_level=previous_high,
        strategy_id=STRATEGY_ID,
        strategy_family=STRATEGY_FAMILY,
        strategy_status=STRATEGY_STATUS,
        market_regime="cross_sectional_reversal",
        strategy_signal_ok=signal_ok,
        strategy_failed_gates=failed,
        take_profit_pct=TAKE_PROFIT_PCT,
        stop_loss_pct=STOP_LOSS_PCT,
    )
