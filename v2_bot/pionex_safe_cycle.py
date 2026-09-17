"""Continuity-enforced wrapper around the paper strategy engine.

The engine uses one actual market book snapshot per processed bar. If a closed
bar was missed, there is no reliable record of the intervening executable bid
or ask. We cannot replay a fictional profitable fill or pretend the gap did not
happen. Mark the strategy HALTED and preserve its last genuinely processed bar.
"""
from __future__ import annotations

from typing import Any

from .pionex_style import Rules, snapshot, step

BAR_MS = 900_000


def safe_step(state: dict[str, Any], candles: list[dict[str, float]],
              hourly: list[dict[str, float]], book: dict[str, float],
              rules: Rules) -> dict[str, Any]:
    if not candles:
        raise RuntimeError("market_candles_missing_fail_closed")
    current = int(candles[-1]["open_time"])
    previous = int(state["last_bar"])
    if previous > 0 and current - previous > BAR_MS:
        state["halted"] = True
        # Do not modify the last genuinely processed bar or create any fill.
        return snapshot(state, book, "missed_closed_candle_halted_manual_reconciliation")
    return step(state, candles, hourly, book, rules)
