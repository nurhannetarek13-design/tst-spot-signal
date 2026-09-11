from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class ShadowOutcome:
    symbol: str
    signal_open_time: float
    entry_price: float
    quantity: float
    take_profit: float
    stop_loss: float
    fee_rate: float
    status: str
    exit_price: float | None
    pnl_usdt: float | None
    reason: str | None
    opened_at: str
    closed_at: str | None


class ShadowOutcomeLedger:
    """Research-only forward outcome ledger for confirmed SHADOW signals.

    It never places orders. Outcomes are evaluated from CLOSED 15m candles.
    If TP and SL are both crossed in the same candle, order is unknowable from
    OHLC data, so the result is marked AMBIGUOUS and excluded from performance
    metrics instead of being counted optimistically.
    """

    TERMINAL = frozenset({"TP", "SL", "AMBIGUOUS"})

    def __init__(self, path: str) -> None:
        self.path = path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_outcomes (
                    symbol TEXT NOT NULL,
                    signal_open_time REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    fee_rate REAL NOT NULL,
                    status TEXT NOT NULL,
                    exit_price REAL,
                    pnl_usdt REAL,
                    reason TEXT,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    PRIMARY KEY (symbol, signal_open_time)
                )
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> ShadowOutcome:
        return ShadowOutcome(**dict(row))

    def open_signal(
        self,
        *,
        symbol: str,
        signal_open_time: float,
        entry_price: float,
        quote_size: float,
        take_profit_pct: float,
        stop_loss_pct: float,
        fee_rate: float,
    ) -> ShadowOutcome:
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol must not be empty")
        if signal_open_time <= 0 or entry_price <= 0 or quote_size <= 0:
            raise ValueError("signal time, entry price and quote size must be > 0")
        if not 0 < take_profit_pct < 1 or not 0 < stop_loss_pct < 1:
            raise ValueError("TP/SL percentages must be between 0 and 1")
        if not 0 <= fee_rate < 0.02:
            raise ValueError("fee_rate must be between 0 and 0.02")

        quantity = quote_size / entry_price
        opened_at = datetime.now(timezone.utc).isoformat()
        outcome = ShadowOutcome(
            symbol=symbol,
            signal_open_time=float(signal_open_time),
            entry_price=float(entry_price),
            quantity=float(quantity),
            take_profit=float(entry_price * (1 + take_profit_pct)),
            stop_loss=float(entry_price * (1 - stop_loss_pct)),
            fee_rate=float(fee_rate),
            status="OPEN",
            exit_price=None,
            pnl_usdt=None,
            reason=None,
            opened_at=opened_at,
            closed_at=None,
        )
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO shadow_outcomes
                    (symbol, signal_open_time, entry_price, quantity, take_profit,
                     stop_loss, fee_rate, status, exit_price, pnl_usdt, reason,
                     opened_at, closed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', NULL, NULL, NULL, ?, NULL)
                    """,
                    (
                        outcome.symbol,
                        outcome.signal_open_time,
                        outcome.entry_price,
                        outcome.quantity,
                        outcome.take_profit,
                        outcome.stop_loss,
                        outcome.fee_rate,
                        outcome.opened_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("shadow_signal_already_tracked") from exc
        return outcome

    def open_outcomes(self) -> list[ShadowOutcome]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM shadow_outcomes WHERE status = 'OPEN' ORDER BY opened_at ASC"
            ).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _net_pnl(outcome: ShadowOutcome, exit_price: float) -> float:
        gross = (exit_price - outcome.entry_price) * outcome.quantity
        entry_fee = outcome.entry_price * outcome.quantity * outcome.fee_rate
        exit_fee = exit_price * outcome.quantity * outcome.fee_rate
        return gross - entry_fee - exit_fee

    def evaluate_closed_candle(
        self,
        outcome: ShadowOutcome,
        candle: dict[str, Any],
    ) -> ShadowOutcome:
        if outcome.status != "OPEN":
            return outcome
        open_time = float(candle.get("open_time", 0) or 0)
        if open_time <= outcome.signal_open_time:
            return outcome
        high = float(candle.get("high", 0) or 0)
        low = float(candle.get("low", 0) or 0)
        if high <= 0 or low <= 0 or high < low:
            raise ValueError("invalid closed candle")

        hit_tp = high >= outcome.take_profit
        hit_sl = low <= outcome.stop_loss
        if not hit_tp and not hit_sl:
            return outcome

        closed_at = datetime.now(timezone.utc).isoformat()
        if hit_tp and hit_sl:
            status = "AMBIGUOUS"
            exit_price = None
            pnl = None
            reason = "tp_and_sl_crossed_same_closed_candle"
        elif hit_tp:
            status = "TP"
            exit_price = outcome.take_profit
            pnl = self._net_pnl(outcome, exit_price)
            reason = "take_profit"
        else:
            status = "SL"
            exit_price = outcome.stop_loss
            pnl = self._net_pnl(outcome, exit_price)
            reason = "stop_loss"

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                UPDATE shadow_outcomes
                SET status = ?, exit_price = ?, pnl_usdt = ?, reason = ?, closed_at = ?
                WHERE symbol = ? AND signal_open_time = ? AND status = 'OPEN'
                """,
                (
                    status,
                    exit_price,
                    pnl,
                    reason,
                    closed_at,
                    outcome.symbol,
                    outcome.signal_open_time,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("shadow_outcome_concurrent_update")

        return ShadowOutcome(
            symbol=outcome.symbol,
            signal_open_time=outcome.signal_open_time,
            entry_price=outcome.entry_price,
            quantity=outcome.quantity,
            take_profit=outcome.take_profit,
            stop_loss=outcome.stop_loss,
            fee_rate=outcome.fee_rate,
            status=status,
            exit_price=exit_price,
            pnl_usdt=pnl,
            reason=reason,
            opened_at=outcome.opened_at,
            closed_at=closed_at,
        )

    def stats(self) -> dict[str, float | int | None]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT status, pnl_usdt FROM shadow_outcomes"
            ).fetchall()

        counts = {"OPEN": 0, "TP": 0, "SL": 0, "AMBIGUOUS": 0}
        winning_pnls: list[float] = []
        losing_pnls: list[float] = []
        for row in rows:
            status = str(row["status"])
            counts[status] = counts.get(status, 0) + 1
            if status not in {"TP", "SL"} or row["pnl_usdt"] is None:
                continue
            pnl = float(row["pnl_usdt"])
            if pnl > 0:
                winning_pnls.append(pnl)
            elif pnl < 0:
                losing_pnls.append(pnl)

        wins = counts.get("TP", 0)
        losses = counts.get("SL", 0)
        decisive = wins + losses
        gross_profit = sum(winning_pnls)
        gross_loss_abs = abs(sum(losing_pnls))
        net_pnl = gross_profit - gross_loss_abs
        profit_factor = (gross_profit / gross_loss_abs) if gross_loss_abs > 0 else (float("inf") if gross_profit > 0 else None)
        expectancy = (net_pnl / decisive) if decisive else None
        avg_win = (gross_profit / len(winning_pnls)) if winning_pnls else None
        avg_loss = (sum(losing_pnls) / len(losing_pnls)) if losing_pnls else None
        terminal = decisive + counts.get("AMBIGUOUS", 0)
        ambiguous_rate = (counts.get("AMBIGUOUS", 0) / terminal) if terminal else None

        return {
            "total": len(rows),
            "open": counts.get("OPEN", 0),
            "wins": wins,
            "losses": losses,
            "ambiguous": counts.get("AMBIGUOUS", 0),
            "decisive": decisive,
            "win_rate": (wins / decisive) if decisive else None,
            "ambiguous_rate": ambiguous_rate,
            "gross_profit_usdt": round(gross_profit, 8),
            "gross_loss_abs_usdt": round(gross_loss_abs, 8),
            "net_pnl_usdt": round(net_pnl, 8),
            "expectancy_usdt": round(expectancy, 8) if expectancy is not None else None,
            "avg_win_usdt": round(avg_win, 8) if avg_win is not None else None,
            "avg_loss_usdt": round(avg_loss, 8) if avg_loss is not None else None,
            "profit_factor": round(profit_factor, 8) if profit_factor not in {None, float("inf")} else profit_factor,
        }
