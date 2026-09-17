"""Fail-closed validation for Binance 15-minute research candles.

Historical results are diagnostic only: passing this gate does not establish
an edge or authorize paper/live execution. No private API is used here.
"""
from __future__ import annotations

import math
from typing import Any

INTERVAL_MS = 15 * 60 * 1000


class ResearchDataError(ValueError):
    """Historical input is incomplete or unsuitable for performance claims."""


def validate_closed_history(
    symbol: str,
    candles: list[dict[str, Any]],
    *,
    start_ms: int,
    end_ms: int,
    as_of_ms: int,
) -> list[dict[str, Any]]:
    """Return complete closed bars only, rejecting gaps, duplication and bad OHLC.

    ``start_ms`` is inclusive and ``end_ms`` is exclusive. A still-forming
    last candle may be dropped; a missing *closed* candle must never be dropped.
    Capture as_of_ms before fetching to keep the requested snapshot stable.
    """
    if not symbol or start_ms < 0 or end_ms <= start_ms or as_of_ms <= 0:
        raise ResearchDataError("invalid_research_history_range")
    cutoff = min(end_ms, as_of_ms)
    first = ((start_ms + INTERVAL_MS - 1) // INTERVAL_MS) * INTERVAL_MS
    last = (cutoff // INTERVAL_MS - 1) * INTERVAL_MS
    if last < first:
        raise ResearchDataError(f"{symbol}:no_complete_requested_candles")

    closed = []
    for raw in candles:
        try:
            opened = int(raw["open_time"])
            close_time = int(raw["close_time"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ResearchDataError(f"{symbol}:invalid_candle_timestamp") from exc
        if opened >= cutoff:
            continue
        if close_time >= cutoff:
            # Only the still-forming final candle can be ignored. Check the
            # complete history separately below so this cannot hide a gap.
            continue
        closed.append(raw)

    expected = first
    for candle in closed:
        try:
            opened = int(candle["open_time"])
            close_time = int(candle["close_time"])
            values = {key: float(candle[key]) for key in
                      ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_quote")}
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ResearchDataError(f"{symbol}:malformed_candle_at_{expected}") from exc
        if opened != expected or opened % INTERVAL_MS or close_time != opened + INTERVAL_MS - 1:
            raise ResearchDataError(f"{symbol}:missing_duplicate_or_misaligned_bar_at_{expected}")
        if not all(math.isfinite(value) for value in values.values()):
            raise ResearchDataError(f"{symbol}:nonfinite_ohlcv_at_{opened}")
        o, h, l, c = (values[k] for k in ("open", "high", "low", "close"))
        if l <= 0 or l > min(o, c) or h < max(o, c) or values["volume"] < 0 or values["quote_volume"] < 0:
            raise ResearchDataError(f"{symbol}:invalid_ohlcv_at_{opened}")
        if values["taker_buy_quote"] < 0 or values["taker_buy_quote"] > values["quote_volume"] * (1 + 1e-8):
            raise ResearchDataError(f"{symbol}:invalid_taker_volume_at_{opened}")
        expected += INTERVAL_MS
    if expected != last + INTERVAL_MS:
        raise ResearchDataError(f"{symbol}:missing_closed_bar_at_{expected}")
    return closed
