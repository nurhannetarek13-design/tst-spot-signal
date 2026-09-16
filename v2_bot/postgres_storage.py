from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .execution_journal import (
    ALL_STAGES,
    ALLOWED_TRANSITIONS,
    PENDING_STAGES,
    TERMINAL_STAGES,
    ExecutionRecord,
)
from .shadow_outcomes import ShadowOutcome
from .state import OpenPosition, PersistenceProbe


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class _PostgresBase:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn.strip()
        if not self.dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("Postgres storage requires a PostgreSQL DSN")
        self._assert_schema()

    def _connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def _assert_schema(self) -> None:
        required = (
            "v2.runtime_meta",
            "v2.positions",
            "v2.trades",
            "v2.emitted_signals",
            "v2.execution_journal",
            "v2.shadow_outcomes",
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass(name) AS relation FROM unnest(%s::text[]) AS name",
                (list(required),),
            )
            rows = cur.fetchall()
        present = {str(row["relation"]) for row in rows if row["relation"] is not None}
        missing = [name for name in required if name not in present]
        if missing:
            raise RuntimeError("POSTGRES_STATE_SCHEMA_MISSING:" + ",".join(missing))


class PostgresStateStore(_PostgresBase):
    def verify_persistence(self, current_revision: str) -> PersistenceProbe:
        revision = current_revision.strip()
        if not revision:
            return PersistenceProbe(False, "deploy_revision_missing", None, "")

        key = "persistence_probe_revision"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM v2.runtime_meta WHERE key = %s FOR UPDATE",
                (key,),
            )
            row = cur.fetchone()
            previous = str(row["value"]) if row else None
            if previous is None:
                cur.execute(
                    "INSERT INTO v2.runtime_meta (key, value, updated_at) VALUES (%s, %s, now())",
                    (key, revision),
                )
                return PersistenceProbe(
                    False,
                    "first_deploy_marker_created",
                    None,
                    revision,
                )
            if previous == revision:
                return PersistenceProbe(
                    False,
                    "same_deploy_revision_not_cross_deploy_proof",
                    previous,
                    revision,
                )
            cur.execute(
                "UPDATE v2.runtime_meta SET value = %s, updated_at = now() WHERE key = %s",
                (revision, key),
            )
        return PersistenceProbe(True, "survived_prior_deployment", previous, revision)

    @staticmethod
    def _position(row: dict[str, Any]) -> OpenPosition:
        return OpenPosition(
            symbol=str(row["symbol"]),
            entry_price=float(row["entry_price"]),
            quantity=float(row["quantity"]),
            quote_size=float(row["quote_size"]),
            take_profit=float(row["take_profit"]),
            stop_loss=float(row["stop_loss"]),
            opened_at=str(_iso(row["opened_at"])),
        )

    def list_open_positions(self) -> list[OpenPosition]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM v2.positions ORDER BY opened_at ASC")
            rows = cur.fetchall()
        return [self._position(row) for row in rows]

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
        opened_dt = datetime.now(timezone.utc)
        position = OpenPosition(
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            quote_size=quote_size,
            take_profit=entry_price * (1.0 + take_profit_pct),
            stop_loss=entry_price * (1.0 - stop_loss_pct),
            opened_at=opened_dt.isoformat(),
        )
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO v2.positions
                    (symbol, entry_price, quantity, quote_size, take_profit, stop_loss, opened_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        position.symbol,
                        position.entry_price,
                        position.quantity,
                        position.quote_size,
                        position.take_profit,
                        position.stop_loss,
                        opened_dt,
                    ),
                )
        except errors.UniqueViolation as exc:
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
        closed_dt = datetime.now(timezone.utc)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM v2.positions WHERE symbol = %s AND opened_at = %s::timestamptz",
                (position.symbol, position.opened_at),
            )
            if cur.rowcount != 1:
                raise RuntimeError("position_not_open_or_stale")
            cur.execute(
                """
                INSERT INTO v2.trades
                (symbol, entry_price, exit_price, quantity, pnl_usdt, reason, opened_at, closed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s::timestamptz, %s)
                """,
                (
                    position.symbol,
                    position.entry_price,
                    exit_price,
                    position.quantity,
                    pnl,
                    reason,
                    position.opened_at,
                    closed_dt,
                ),
            )
        return pnl

    def claim_signal(self, *, symbol: str, signal_open_time: float, kind: str) -> bool:
        if not symbol or not kind:
            raise ValueError("symbol and kind must not be empty")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO v2.emitted_signals (symbol, signal_open_time, kind, created_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (symbol, signal_open_time, kind) DO NOTHING
                RETURNING symbol
                """,
                (symbol, signal_open_time, kind),
            )
            inserted = cur.fetchone()
        return inserted is not None

    def realized_pnl_today(self) -> float:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(pnl_usdt), 0) AS pnl
                FROM v2.trades
                WHERE closed_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                  AND closed_at < (date_trunc('day', now() AT TIME ZONE 'UTC') + interval '1 day') AT TIME ZONE 'UTC'
                """
            )
            row = cur.fetchone()
        return float(row["pnl"] if row else 0.0)


class PostgresShadowOutcomeLedger(_PostgresBase):
    TERMINAL = frozenset({"TP", "SL", "AMBIGUOUS"})

    @staticmethod
    def _row(row: dict[str, Any]) -> ShadowOutcome:
        return ShadowOutcome(
            symbol=str(row["symbol"]),
            signal_open_time=float(row["signal_open_time"]),
            entry_price=float(row["entry_price"]),
            quantity=float(row["quantity"]),
            take_profit=float(row["take_profit"]),
            stop_loss=float(row["stop_loss"]),
            fee_rate=float(row["fee_rate"]),
            status=str(row["status"]),
            exit_price=float(row["exit_price"]) if row["exit_price"] is not None else None,
            pnl_usdt=float(row["pnl_usdt"]) if row["pnl_usdt"] is not None else None,
            reason=str(row["reason"]) if row["reason"] is not None else None,
            opened_at=str(_iso(row["opened_at"])),
            closed_at=_iso(row["closed_at"]),
        )

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
        opened_dt = datetime.now(timezone.utc)
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
            opened_at=opened_dt.isoformat(),
            closed_at=None,
        )
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO v2.shadow_outcomes
                    (symbol, signal_open_time, entry_price, quantity, take_profit,
                     stop_loss, fee_rate, status, exit_price, pnl_usdt, reason,
                     opened_at, closed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'OPEN', NULL, NULL, NULL, %s, NULL)
                    """,
                    (
                        outcome.symbol,
                        outcome.signal_open_time,
                        outcome.entry_price,
                        outcome.quantity,
                        outcome.take_profit,
                        outcome.stop_loss,
                        outcome.fee_rate,
                        opened_dt,
                    ),
                )
        except errors.UniqueViolation as exc:
            raise RuntimeError("shadow_signal_already_tracked") from exc
        return outcome

    def open_outcomes(self) -> list[ShadowOutcome]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM v2.shadow_outcomes WHERE status = 'OPEN' ORDER BY opened_at ASC"
            )
            rows = cur.fetchall()
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

        closed_dt = datetime.now(timezone.utc)
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

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE v2.shadow_outcomes
                SET status = %s, exit_price = %s, pnl_usdt = %s, reason = %s, closed_at = %s
                WHERE symbol = %s AND signal_open_time = %s AND status = 'OPEN'
                """,
                (
                    status,
                    exit_price,
                    pnl,
                    reason,
                    closed_dt,
                    outcome.symbol,
                    outcome.signal_open_time,
                ),
            )
            if cur.rowcount != 1:
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
            closed_at=closed_dt.isoformat(),
        )

    def stats(self) -> dict[str, float | int | None]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT status, pnl_usdt FROM v2.shadow_outcomes")
            rows = cur.fetchall()

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
        profit_factor = (gross_profit / gross_loss_abs) if gross_loss_abs > 0 else None
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
            "profit_factor": round(profit_factor, 8) if profit_factor is not None else None,
        }


class PostgresExecutionJournal(_PostgresBase):
    @staticmethod
    def _validate_stage(stage: str) -> str:
        normalized = stage.strip().upper()
        if normalized not in ALL_STAGES:
            raise ValueError(f"invalid execution stage: {stage}")
        return normalized

    @staticmethod
    def _row(row: dict[str, Any]) -> ExecutionRecord:
        details = row.get("details_json")
        if not isinstance(details, dict):
            details = {"value": details}
        return ExecutionRecord(
            client_order_id=str(row["client_order_id"]),
            symbol=str(row["symbol"]),
            stage=str(row["stage"]),
            details=details,
            created_at=str(_iso(row["created_at"])),
            updated_at=str(_iso(row["updated_at"])),
        )

    def begin(
        self,
        *,
        client_order_id: str,
        symbol: str,
        details: dict[str, Any] | None = None,
    ) -> ExecutionRecord:
        order_id = client_order_id.strip()
        symbol = symbol.strip().upper()
        if not order_id:
            raise ValueError("client_order_id must not be empty")
        if not symbol:
            raise ValueError("symbol must not be empty")
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO v2.execution_journal
                    (client_order_id, symbol, stage, details_json, created_at, updated_at)
                    VALUES (%s, %s, 'INTENT', %s, now(), now())
                    """,
                    (order_id, symbol, Jsonb(details or {})),
                )
        except errors.UniqueViolation as exc:
            raise RuntimeError("execution_intent_already_exists") from exc
        record = self.get(order_id)
        if record is None:
            raise RuntimeError("execution_journal_write_failed")
        return record

    def transition(
        self,
        client_order_id: str,
        stage: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> ExecutionRecord:
        order_id = client_order_id.strip()
        normalized_stage = self._validate_stage(stage)
        current = self.get(order_id)
        if current is None:
            raise RuntimeError("execution_intent_not_found")
        if normalized_stage != current.stage:
            allowed = ALLOWED_TRANSITIONS.get(current.stage, frozenset())
            if normalized_stage not in allowed:
                if current.stage in TERMINAL_STAGES:
                    raise RuntimeError("execution_terminal_state_locked")
                raise RuntimeError(
                    f"invalid_execution_transition:{current.stage}->{normalized_stage}"
                )

        merged_details = dict(current.details)
        if details:
            merged_details.update(details)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE v2.execution_journal
                SET stage = %s, details_json = %s, updated_at = now()
                WHERE client_order_id = %s AND stage = %s
                """,
                (normalized_stage, Jsonb(merged_details), order_id, current.stage),
            )
            if cur.rowcount != 1:
                raise RuntimeError("execution_journal_concurrent_update")
        record = self.get(order_id)
        if record is None:
            raise RuntimeError("execution_journal_readback_failed")
        return record

    def get(self, client_order_id: str) -> ExecutionRecord | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM v2.execution_journal WHERE client_order_id = %s",
                (client_order_id.strip(),),
            )
            row = cur.fetchone()
        return self._row(row) if row else None

    def pending(self) -> list[ExecutionRecord]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM v2.execution_journal
                WHERE stage = ANY(%s::text[])
                ORDER BY created_at ASC
                """,
                (sorted(PENDING_STAGES),),
            )
            rows = cur.fetchall()
        return [self._row(row) for row in rows]

    def has_pending(self) -> bool:
        return bool(self.pending())
