"""Fail-closed recovery of V2 SHADOW outcomes after an interrupted worker.

Reads public, CLOSED Binance 15m candles only. Never creates an order or
changes Live/Paper authorization. A missing historical candle blocks accounting
rather than pretending the latest eight candles describe the entire trade.
"""
from __future__ import annotations

import time
from typing import Any

from .shadow_tournament import decode_shadow_key
from .tournament_engine import TournamentRuntimeV2Engine

CANDLE_MS = 15 * 60 * 1000
MAX_KLINES = 1000


def replay_open_outcome(*, outcome: Any, market: Any, ledger: Any,
                        now_ms: int | None = None) -> dict[str, Any] | None:
    """Return a close/gap event, or None while a position remains unclosed.

    No candle is evaluated until *all* candles since the signal are verified
    consecutive and closed. An unrecoverable >998-bar gap stays OPEN and is
    explicitly reported for data repair; it is never promoted as a winner.
    """
    symbol, strategy_id = decode_shadow_key(outcome.symbol)
    base = {"symbol": symbol, "strategy_id": strategy_id,
            "signal_open_time": outcome.signal_open_time}

    def blocked(reason: str) -> dict[str, Any]:
        return {"event": "shadow_outcome_gap_blocked", **base, "reason": reason}

    signal_ms = int(outcome.signal_open_time)
    if signal_ms <= 0 or signal_ms % CANDLE_MS != 0:
        return blocked("invalid_signal_candle_timestamp")
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    last_closed_open = (now // CANDLE_MS - 1) * CANDLE_MS
    next_open = signal_ms + CANDLE_MS
    if next_open > last_closed_open:
        return None  # Next complete candle is not available yet.

    needed = (last_closed_open - signal_ms) // CANDLE_MS + 2
    if needed > MAX_KLINES:
        return blocked("history_exceeds_public_kline_limit")

    candles = market.klines(symbol, "15m", int(max(8, needed)))
    relevant = sorted(
        (candle for candle in candles if int(candle.get("open_time", -1)) >= next_open),
        key=lambda candle: int(candle["open_time"]),
    )
    expected = next_open
    for candle in relevant:
        opened = int(candle["open_time"])
        if opened != expected or opened > last_closed_open:
            return blocked(f"non_contiguous_or_unclosed_candle_at_{expected}")
        expected += CANDLE_MS
    if expected <= last_closed_open:
        return blocked(f"missing_closed_candle_at_{expected}")

    current = outcome
    for candle in relevant:
        current = ledger.evaluate_closed_candle(current, candle)
        if current.status != "OPEN":
            return {
                "event": "shadow_outcome_closed", **base,
                "status": current.status, "reason": current.reason,
                "exit_price": current.exit_price,
                "pnl_usdt_net_fees": current.pnl_usdt,
            }
    return None


class ContinuitySafeTournamentRuntimeV2Engine(TournamentRuntimeV2Engine):
    """Same research tournament, with gap-safe forward outcome accounting."""

    def _manage_shadow_outcomes(self) -> list[dict[str, Any]]:
        if self.settings.mode != "shadow" or self.shadow_outcomes is None:
            return []
        events = []
        for outcome in self.shadow_outcomes.open_outcomes():
            symbol, strategy_id = decode_shadow_key(outcome.symbol)
            try:
                event = replay_open_outcome(
                    outcome=outcome, market=self.market, ledger=self.shadow_outcomes,
                )
                if event is not None:
                    events.append(event)
            except Exception as exc:
                events.append({
                    "event": "shadow_outcome_error", "symbol": symbol,
                    "strategy_id": strategy_id,
                    "signal_open_time": outcome.signal_open_time,
                    "error_type": type(exc).__name__, "error": str(exc),
                })
        return events
