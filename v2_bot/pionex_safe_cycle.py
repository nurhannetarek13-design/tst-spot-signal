"""Continuity-aware wrapper around the credential-free paper strategy engine.

No invented fills are allowed during missed market bars. An exposed strategy
halts for manual reconciliation; a FLAT strategy may safely skip old entry
signals, clear its stale anchor and resume on the NEXT closed candle.
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
        if state["lots"] or state["halted"]:
            # An open position could have crossed a stop/target while offline.
            # Never fabricate the intrabar execution or auto-clear an old halt.
            state["halted"] = True
            return snapshot(state, book, "missed_closed_candle_halted_manual_reconciliation")
        # With NO inventory there is no unknown exit or missing trade to replay.
        # Skip all old entry opportunities, erase the stale grid anchor, and
        # allow the NEXT distinct candle to start a normal decision cycle.
        state["last_bar"] = current
        if state["mode"] == "spot_grid":
            state["anchor"] = None
        state["events"] = (state["events"] + [{"bar": current,
            "action": "flat_gap_resynchronized_no_trade"}])[-100:]
        return snapshot(state, book, "flat_gap_resynchronized_no_trade")
    return step(state, candles, hourly, book, rules)
