from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


PENDING_STAGES = frozenset(
    {
        "INTENT",
        "BUY_UNKNOWN",
        "BUY_FILLED",
        "OCO_INTENT",
        "OCO_UNKNOWN",
        "UNPROTECTED",
    }
)
TERMINAL_STAGES = frozenset({"PROTECTED", "FLATTENED", "ABORTED"})
ALL_STAGES = PENDING_STAGES | TERMINAL_STAGES
ALLOWED_TRANSITIONS = {
    "INTENT": frozenset({"BUY_UNKNOWN", "BUY_FILLED", "ABORTED"}),
    "BUY_UNKNOWN": frozenset({"BUY_FILLED", "ABORTED"}),
    "BUY_FILLED": frozenset({"OCO_INTENT", "UNPROTECTED", "FLATTENED"}),
    "OCO_INTENT": frozenset({"OCO_UNKNOWN", "PROTECTED", "UNPROTECTED", "FLATTENED"}),
    "OCO_UNKNOWN": frozenset({"PROTECTED", "UNPROTECTED", "FLATTENED"}),
    "UNPROTECTED": frozenset({"PROTECTED", "FLATTENED"}),
    "PROTECTED": frozenset(),
    "FLATTENED": frozenset(),
    "ABORTED": frozenset(),
}


@dataclass(frozen=True)
class ExecutionRecord:
    client_order_id: str
    symbol: str
    stage: str
    details: dict[str, Any]
    created_at: str
    updated_at: str


class ExecutionJournal:
    """Durable crash-recovery journal for future live Spot execution.

    A record is created before the first private Binance request. Any
    non-terminal stage blocks a fresh live execution until reconciliation
    resolves the old attempt. State transitions are monotonic so a terminal
    execution can never silently become pending again.
    """

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
                CREATE TABLE IF NOT EXISTS execution_journal (
                    client_order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _validate_stage(stage: str) -> str:
        normalized = stage.strip().upper()
        if normalized not in ALL_STAGES:
            raise ValueError(f"invalid execution stage: {stage}")
        return normalized

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ExecutionRecord:
        try:
            details = json.loads(str(row["details_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            details = {"journal_decode_error": True}
        if not isinstance(details, dict):
            details = {"value": details}
        return ExecutionRecord(
            client_order_id=str(row["client_order_id"]),
            symbol=str(row["symbol"]),
            stage=str(row["stage"]),
            details=details,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
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

        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(details or {}, sort_keys=True, separators=(",", ":"))
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO execution_journal
                    (client_order_id, symbol, stage, details_json, created_at, updated_at)
                    VALUES (?, ?, 'INTENT', ?, ?, ?)
                    """,
                    (order_id, symbol, payload, now, now),
                )
        except sqlite3.IntegrityError as exc:
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
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(merged_details, sort_keys=True, separators=(",", ":"))

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                UPDATE execution_journal
                SET stage = ?, details_json = ?, updated_at = ?
                WHERE client_order_id = ? AND stage = ?
                """,
                (normalized_stage, payload, now, order_id, current.stage),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("execution_journal_concurrent_update")

        record = self.get(order_id)
        if record is None:
            raise RuntimeError("execution_journal_readback_failed")
        return record

    def get(self, client_order_id: str) -> ExecutionRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM execution_journal WHERE client_order_id = ?",
                (client_order_id.strip(),),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def pending(self) -> list[ExecutionRecord]:
        placeholders = ",".join("?" for _ in PENDING_STAGES)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM execution_journal WHERE stage IN ({placeholders}) ORDER BY created_at ASC",
                tuple(sorted(PENDING_STAGES)),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def has_pending(self) -> bool:
        return bool(self.pending())
