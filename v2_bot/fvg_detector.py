"""Research-only, reproducible bullish FVG detection on CLOSED Binance Spot bars.

Classical 3-bar imbalance: older.high < newer.low. The middle impulse and
third bar must be closed before the zone exists; a later wick tests it and a
subsequent closed confirmation triggers an observable-book PAPER entry.
No assertion is made that the pattern is predictive or profitable.
"""
from __future__ import annotations

import math
from statistics import fmean
from typing import Any

BAR_MS = 900_000


def bullish_fvg(older: dict[str, float], middle: dict[str, float],
                newer: dict[str, float], *, min_gap: float = 0.0005) -> dict[str, float] | None:
    """Return a fully-formed bullish three-CLOSED-bar FVG, never an unclosed bar."""
    bars = (older, middle, newer)
    try:
        for bar in bars:
            if not all(math.isfinite(float(bar[k])) for k in
                       ("open_time", "open", "high", "low", "close")):
                return None
            if not (0 < bar["low"] <= min(bar["open"], bar["close"])
                    <= max(bar["open"], bar["close"]) <= bar["high"]):
                return None
        if not (int(middle["open_time"]) - int(older["open_time"]) == BAR_MS
                and int(newer["open_time"]) - int(middle["open_time"]) == BAR_MS):
            return None
        lower, upper = float(older["high"]), float(newer["low"])
        if not (upper > lower and (upper - lower) / lower >= min_gap
                and (upper - lower) / lower <= .025
                and middle["close"] > middle["open"]
                and middle["close"] > lower
                and newer["close"] > newer["open"]):
            return None
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None
    return {"lower": lower, "upper": upper,
            "formed_bar": int(newer["open_time"]),
            "gap_pct": (upper / lower - 1) * 100}


def detect_crash_fvg_retest(candles: list[dict[str, float]], *, ask: float,
                            bid: float) -> dict[str, Any] | None:
    """Frozen experimental seven-bar sequence, on closed bars only.

    [-7] crash, [-6] bullish reversal (FVG older), [-5] upward impulse,
    [-4] FVG completion, [-3] holding candle, [-2] first wick retest,
    [-1] confirmation close above FVG/retest highs. No future candles used.
    This intentionally differs from the four-bar baseline; compare them
    prospectively on identical public market snapshots, NOT matched trades.
    """
    if len(candles) < 80 or not all(math.isfinite(x) for x in (ask, bid)):
        return None
    if not (0 < bid <= ask and (ask - bid) / bid <= .003):
        return None
    try:
        crash, rev, impulse, formed, hold, retest, confirm = candles[-7:]
        prior = candles[-27:-7]
        # True range uses the PREVIOUS candle close, not the present close.
        true_ranges = [max(float(x["high"]) - float(x["low"]),
                           abs(float(x["high"]) - float(candles[i-1]["close"])),
                           abs(float(x["low"]) - float(candles[i-1]["close"])))
                       for i, x in enumerate(candles[-27:-7], start=len(candles)-27)]
        atr = fmean(true_ranges)
        if not math.isfinite(atr) or atr <= 0:
            return None
        if not (crash["close"] <= candles[-15]["close"] * .97
                and crash["close"] <= crash["open"] * .985
                and crash["high"] - crash["low"] >= 1.25 * atr):
            return None
        if not (rev["close"] > rev["open"] and rev["close"] > crash["close"]
                and rev["close"] >= (rev["high"] + rev["low"]) / 2):
            return None
        zone = bullish_fvg(rev, impulse, formed)
        if zone is None or zone["upper"] - zone["lower"] < .10 * atr:
            return None
        # The hold may touch the upper boundary, but must not fully fill the gap.
        if not (hold["low"] > zone["lower"] and hold["close"] >= zone["upper"]):
            return None
        wick = min(retest["open"], retest["close"]) - retest["low"]
        if not (zone["lower"] <= retest["low"] <= zone["upper"]
                and retest["close"] > zone["upper"]
                and wick >= .5 * abs(retest["close"] - retest["open"])):
            return None
        if not (confirm["close"] > max(formed["high"], retest["high"])
                and confirm["close"] > confirm["open"]
                and confirm["quote_volume"] >= 1.2 * fmean(x["quote_volume"] for x in prior)):
            return None
        if ask > confirm["close"] * 1.005:
            return None
        # Import constants rather than silently changing the baseline cost model.
        from .pionex_style import FEE, SLIPPAGE
        entry = ask * (1 + SLIPPAGE)
        stop = zone["lower"] - .15 * atr
        risk = entry - stop
        if not (.005 <= risk / entry <= .04):
            return None
        target = entry + 1.2 * risk
        if (target / entry - 1) <= 2 * (FEE + SLIPPAGE) + (ask - bid) / bid + .002:
            return None
        return {**zone, "entry": entry, "stop": stop, "target": target,
                "risk_per_unit": risk, "confirm_bar": int(confirm["open_time"]),
                "retest_bar": int(retest["open_time"]), "atr": atr,
                "signal": "crash_reversal_bullish_fvg_retest_confirmed"}
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None
