from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .binance_private import BinanceAPIError, BinanceSignedSpotClient
from .execution_journal import ExecutionJournal, ExecutionRecord


@dataclass(frozen=True)
class ReconciliationItem:
    client_order_id: str
    symbol: str
    journal_stage: str
    classification: str
    order_status: str | None
    order_list_status: str | None
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "journal_stage": self.journal_stage,
            "classification": self.classification,
            "order_status": self.order_status,
            "order_list_status": self.order_list_status,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class ReconciliationReport:
    pending_count: int
    unresolved_count: int
    can_trade: bool | None
    items: tuple[ReconciliationItem, ...]

    @property
    def clear(self) -> bool:
        return self.pending_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending_count": self.pending_count,
            "unresolved_count": self.unresolved_count,
            "can_trade": self.can_trade,
            "clear": self.clear,
            "items": [item.to_dict() for item in self.items],
        }


class ReadOnlyExecutionReconciler:
    """Inspect pending journal entries against Binance without mutating state.

    This component deliberately does not transition the execution journal and
    does not place/cancel orders. Its output is evidence for a separate,
    explicitly reviewed recovery decision.
    """

    def __init__(
        self,
        *,
        client: BinanceSignedSpotClient,
        journal: ExecutionJournal,
    ) -> None:
        self.client = client
        self.journal = journal

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        try:
            return Decimal(str(value))
        except Exception:
            return Decimal("0")

    @staticmethod
    def _safe_api_error(exc: Exception) -> dict[str, Any]:
        if isinstance(exc, BinanceAPIError):
            return {
                "error_type": type(exc).__name__,
                "status_code": exc.status_code,
                "code": exc.code,
            }
        return {"error_type": type(exc).__name__}

    def _query_order(self, record: ExecutionRecord) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        try:
            order = self.client.query_order(
                symbol=record.symbol,
                client_order_id=record.client_order_id,
            )
            return order, {}
        except Exception as exc:
            return None, {"order_query": self._safe_api_error(exc)}

    def _query_order_list(
        self,
        record: ExecutionRecord,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        list_client_order_id = str(
            record.details.get("oco_list_client_order_id", "")
        ).strip()
        if not list_client_order_id:
            return None, {"order_list_query": {"reason": "list_client_order_id_missing"}}
        try:
            order_list = self.client.query_order_list(
                list_client_order_id=list_client_order_id,
            )
            return order_list, {"list_client_order_id": list_client_order_id}
        except Exception as exc:
            return None, {
                "list_client_order_id": list_client_order_id,
                "order_list_query": self._safe_api_error(exc),
            }

    def _inspect_record(self, record: ExecutionRecord) -> ReconciliationItem:
        evidence: dict[str, Any] = {}
        order, order_evidence = self._query_order(record)
        evidence.update(order_evidence)
        order_status = str(order.get("status", "")).upper() if order else None
        executed_qty = self._decimal(order.get("executedQty")) if order else Decimal("0")
        if order is not None:
            evidence["executed_qty"] = format(executed_qty, "f")
            evidence["order_id"] = str(order.get("orderId", ""))

        order_list = None
        order_list_status = None
        if record.stage in {"BUY_FILLED", "OCO_INTENT", "OCO_UNKNOWN", "UNPROTECTED"}:
            order_list, list_evidence = self._query_order_list(record)
            evidence.update(list_evidence)
            if order_list is not None:
                order_list_status = str(order_list.get("listOrderStatus", "")).upper()
                evidence["order_list_id"] = str(order_list.get("orderListId", ""))

        if order_list_status in {"EXECUTING", "ALL_DONE"}:
            classification = "protection_order_list_found"
        elif order_status == "FILLED" and executed_qty > 0:
            classification = "filled_buy_requires_protection_review"
        elif order_status in {"REJECTED", "EXPIRED", "CANCELED"} and executed_qty <= 0:
            classification = "terminal_zero_fill_candidate_for_abort"
        elif order is None:
            classification = "buy_state_not_confirmed"
        else:
            classification = "manual_recovery_required"

        return ReconciliationItem(
            client_order_id=record.client_order_id,
            symbol=record.symbol,
            journal_stage=record.stage,
            classification=classification,
            order_status=order_status,
            order_list_status=order_list_status,
            evidence=evidence,
        )

    def inspect(self) -> ReconciliationReport:
        pending = self.journal.pending()
        if not pending:
            return ReconciliationReport(
                pending_count=0,
                unresolved_count=0,
                can_trade=None,
                items=(),
            )

        can_trade: bool | None = None
        try:
            account = self.client.account_information(omit_zero_balances=True)
            raw_can_trade = account.get("canTrade")
            if isinstance(raw_can_trade, bool):
                can_trade = raw_can_trade
        except Exception:
            # Account snapshot is supporting evidence only. Per-order queries
            # below remain useful even if the account endpoint is unavailable.
            can_trade = None

        items = tuple(self._inspect_record(record) for record in pending)
        return ReconciliationReport(
            pending_count=len(pending),
            # Any pending journal entry remains unresolved until a separately
            # reviewed recovery action transitions it to a terminal stage.
            unresolved_count=len(pending),
            can_trade=can_trade,
            items=items,
        )
