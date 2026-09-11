from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    entry_price: float
    quantity: float
    quote_size: float
    take_profit: float
    stop_loss: float
    opened_at: str


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    entry_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    quote_size REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    opened_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    pnl_usdt REAL NOT NULL,
                    reason TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL
                )
                """
            )

    def list_open_positions(self) -> list[OpenPosition]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM positions ORDER BY opened_at ASC").fetchall()
        return [OpenPosition(**dict(row)) for row in rows]

    def open_position(
        self,
        *,
        symbol: str,
        entry_price: float,
        quote_size: float,
        take_profit_pct: float,
        stop_loss_pct: float,
    ) -> OpenPosition:
        quantity = quote_size / entry_price
        opened_at = datetime.now(timezone.utc).isoformat()
        position = OpenPosition(
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            quote_size=quote_size,
            take_profit=entry_price * (1.0 + take_profit_pct),
            stop_loss=entry_price * (1.0 - stop_loss_pct),
            opened_at=opened_at,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO positions
                (symbol, entry_price, quantity, quote_size, take_profit, stop_loss, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.symbol,
                    position.entry_price,
                    position.quantity,
                    position.quote_size,
                    position.take_profit,
                    position.stop_loss,
                    position.opened_at,
                ),
            )
        return position

    def close_position(
        self,
        position: OpenPosition,
        *,
        exit_price: float,
        reason: str,
        fee_rate: float,
    ) -> float:
        gross_pnl = (exit_price - position.entry_price) * position.quantity
        entry_fee = position.entry_price * position.quantity * fee_rate
        exit_fee = exit_price * position.quantity * fee_rate
        pnl = gross_pnl - entry_fee - exit_fee
        closed_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM positions WHERE symbol = ?", (position.symbol,))
            conn.execute(
                """
                INSERT INTO trades
                (symbol, entry_price, exit_price, quantity, pnl_usdt, reason, opened_at, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.symbol,
                    position.entry_price,
                    exit_price,
                    position.quantity,
                    pnl,
                    reason,
                    position.opened_at,
                    closed_at,
                ),
            )
        return pnl

    def realized_pnl_today(self) -> float:
        day_prefix = datetime.now(timezone.utc).date().isoformat() + "%"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdt), 0.0) AS pnl FROM trades WHERE closed_at LIKE ?",
                (day_prefix,),
            ).fetchone()
        return float(row["pnl"] if row else 0.0)
