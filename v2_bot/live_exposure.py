from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class LiveExposureEvent:
    client_order_id: str
    symbol: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "status": self.status,
            "detail": self.detail,
        }


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def reconcile_active_protected(*, client: Any, journal: Any) -> list[LiveExposureEvent]:
    """Reconcile durable PROTECTED exposure without placing/canceling orders.

    PROTECTED means the entry is covered by an OCO but the Spot exposure is
    still open. Only a verified filled child SELL order moves it to CLOSED.
    If Binance says the list is ALL_DONE but no child SELL can be proven filled,
    the record becomes UNPROTECTED and blocks all new Live entries.
    """

    events: list[LiveExposureEvent] = []
    for record in journal.active_protected():
        list_client_id = str(record.details.get("oco_list_client_order_id", "")).strip()
        if not list_client_id:
            journal.transition(
                record.client_order_id,
                "UNPROTECTED",
                details={"exposure_reconcile_reason": "oco_list_client_order_id_missing"},
            )
            events.append(
                LiveExposureEvent(
                    record.client_order_id,
                    record.symbol,
                    "UNPROTECTED",
                    "oco_list_client_order_id_missing",
                )
            )
            continue

        try:
            order_list = client.query_order_list(list_client_order_id=list_client_id)
        except Exception as exc:
            events.append(
                LiveExposureEvent(
                    record.client_order_id,
                    record.symbol,
                    "PROTECTED_UNRESOLVED",
                    f"order_list_query_failed:{type(exc).__name__}",
                )
            )
            continue

        list_status = str(order_list.get("listOrderStatus", "")).upper()
        if list_status == "EXECUTING":
            events.append(
                LiveExposureEvent(
                    record.client_order_id,
                    record.symbol,
                    "PROTECTED",
                    "oco_executing",
                )
            )
            continue

        if list_status != "ALL_DONE":
            events.append(
                LiveExposureEvent(
                    record.client_order_id,
                    record.symbol,
                    "PROTECTED_UNRESOLVED",
                    f"unexpected_list_status:{list_status or 'EMPTY'}",
                )
            )
            continue

        child_orders = order_list.get("orders") or []
        client_ids = [
            str(row.get("clientOrderId", "")).strip()
            for row in child_orders
            if isinstance(row, dict) and str(row.get("clientOrderId", "")).strip()
        ]
        if not client_ids:
            journal.transition(
                record.client_order_id,
                "UNPROTECTED",
                details={
                    "exposure_reconcile_reason": "oco_all_done_without_child_order_ids",
                    "oco_list_status": list_status,
                },
            )
            events.append(
                LiveExposureEvent(
                    record.client_order_id,
                    record.symbol,
                    "UNPROTECTED",
                    "oco_all_done_without_child_order_ids",
                )
            )
            continue

        filled_children: list[dict[str, Any]] = []
        query_failed = False
        for child_client_id in client_ids:
            try:
                child = client.query_order(
                    symbol=record.symbol,
                    client_order_id=child_client_id,
                )
            except Exception:
                query_failed = True
                break
            if (
                str(child.get("status", "")).upper() == "FILLED"
                and str(child.get("side", "")).upper() == "SELL"
                and _decimal(child.get("executedQty")) > 0
            ):
                filled_children.append(child)

        if query_failed:
            events.append(
                LiveExposureEvent(
                    record.client_order_id,
                    record.symbol,
                    "PROTECTED_UNRESOLVED",
                    "child_order_query_failed",
                )
            )
            continue

        if len(filled_children) == 1:
            filled = filled_children[0]
            journal.transition(
                record.client_order_id,
                "CLOSED",
                details={
                    "oco_list_status": list_status,
                    "exit_client_order_id": str(filled.get("clientOrderId", "")),
                    "exit_order_id": str(filled.get("orderId", "")),
                    "exit_executed_qty": str(filled.get("executedQty", "")),
                    "exit_cumulative_quote_qty": str(filled.get("cummulativeQuoteQty", "")),
                    "exit_verified": True,
                },
            )
            events.append(
                LiveExposureEvent(
                    record.client_order_id,
                    record.symbol,
                    "CLOSED",
                    "verified_filled_oco_exit",
                )
            )
            continue

        journal.transition(
            record.client_order_id,
            "UNPROTECTED",
            details={
                "exposure_reconcile_reason": (
                    "oco_all_done_without_filled_sell"
                    if not filled_children
                    else "multiple_filled_sell_children"
                ),
                "oco_list_status": list_status,
            },
        )
        events.append(
            LiveExposureEvent(
                record.client_order_id,
                record.symbol,
                "UNPROTECTED",
                "oco_all_done_exit_not_safely_resolved",
            )
        )

    return events
