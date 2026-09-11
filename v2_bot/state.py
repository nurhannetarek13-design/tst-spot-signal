from __future__ import annotations

import sqlite3
from contextlib import closing
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


@dataclass(frozen=True)
class PersistenceProbe:
    proven: bool
    reason: str
    previous_revision: str | None
    current_revision: str


class StateStore:
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS emitted_signals (
                    symbol TEXT NOT NULL,
                    signal_open_time REAL NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, signal_open_time, kind)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def verify_persistence(self, current_revision: str) -> PersistenceProbe:
        revision = current_revision.strip()
        if not revision:
            return PersistenceProbe(
                proven=False,
                reason="deploy_revision_missing",
                previous_revision=None,
                current_revision="",
            )

        now = datetime.now(timezone.utc).isoformat()
        key = "persistence_probe_revision"
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT value FROM runtime_meta WHERE key = ?",
                (key,),
            ).fetchone()
            previous = str(row["value"]) if row else None

            if previous is None:
                conn.execute(
                    "INSERT INTO runtime_meta (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, revision, now),
                )
                return PersistenceProbe(
                    proven=False,
                    reason="first_deploy_marker_created",
                    previous_revision=None,
                    current_revision=revision,
                )

            if previous == revision:
                return PersistenceProbe(
                    proven=False,
                    reason="same_deploy_revision_not_cross_deploy_proof",
                    previous_revision=previous,
                    current_revision=revision,
                )

            conn.execute(
                "UPDATE runtime_meta SET value = ?, updated_at = ? WHERE key = ?",
                (revision, now, key),
            )
            return PersistenceProbe(
                proven=True,
                reason="survived_prior_deployment",
                previous_revision=previous,
                current_revision=revision,
            )

    def list_open_positions(self) -> list[OpenPosition]:
        with closing(self._connect()) as conn:
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
        if not symbol:
            raise ValueError("symbol must not be empty")
        if entry_price <= 0:
            raise ValueError("entry_price must be > 0")
        if quote_size <= 0:
            raise ValueError("quote_size must be > 0")
        if not 0 < take_profit_pct < 1:
            raise ValueError("take_profit_pct must be between 0 and 1")
        if not 0 < stop_loss_pct < 1:
            raise ValueError("stop_loss_pct must be between 0 and 1")

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
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO positions
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
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("position_already_open") from exc
        return position

    def close_position(
        self,
        position: OpenPosition,
        *,
        exit_price: float,
        reason: str,
        fee_rate: float,
    ) -> float:
        if exit_price <= 0:
            raise ValueError("exit_price must be > 0")
        if not reason:
            raise ValueError("reason must not be empty")
        if not 0 <= fee_rate < 0.02:
            raise ValueError("fee_rate must be between 0 and 0.02")

        gross_pnl = (exit_price - position.entry_price) * position.quantity
        entry_fee = position.entry_price * position.quantity * fee_rate
        exit_fee = exit_price * position.quantity * fee_rate
        pnl = gross_pnl - entry_fee - exit_fee
        closed_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "DELETE FROM positions WHERE symbol = ? AND opened_at = ?",
                (position.symbol, position.opened_at),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("position_not_open_or_stale")
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

    def claim_signal(self, *, symbol: str, signal_open_time: float, kind: str) -> bool:
        if not symbol or not kind:
            raise ValueError("symbol and kind must not be empty")
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO emitted_signals (symbol, signal_open_time, kind, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (symbol, signal_open_time, kind, created_at),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def realized_pnl_today(self) -> float:
        day_prefix = datetime.now(timezone.utc).date().isoformat() + "%"
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdt), 0.0) AS pnl FROM trades WHERE closed_at LIKE ?",
                (day_prefix,),
            ).fetchone()
        return float(row["pnl"] if row else 0.0)
