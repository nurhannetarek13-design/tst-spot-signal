from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

import httpx

from .binance_private import BinanceAPIError, BinanceSignedSpotClient
from .execution_journal import ExecutionJournal


class RecoveryRequired(RuntimeError):
    """Raised when exchange state is not certain enough to continue safely."""


@dataclass(frozen=True)
class ProtectedExecutionResult:
    symbol: str
    client_order_id: str
    list_client_order_id: str
    protected_quantity: Decimal
    status: str


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def _ids(symbol: str, signal_key: str) -> dict[str, str]:
    seed = f"{symbol.upper()}|{signal_key}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()[:20]
    return {
        "buy": f"v2b{digest}",
        "oco": f"v2o{digest}",
        "tp": f"v2t{digest}",
        "sl": f"v2s{digest}",
        "flat": f"v2f{digest}",
    }


class ProtectedSpotExecutor:
    """Future live executor with idempotency and crash-safe journaling.

    It is intentionally not wired into V2Engine yet. Live remains hard-locked
    until persistent storage, account reconciliation, and explicit credentials
    are available in the isolated runtime.
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
    def _is_uncertain_error(exc: BaseException) -> bool:
        if isinstance(exc, httpx.TransportError):
            return True
        return isinstance(exc, BinanceAPIError) and exc.execution_unknown

    @staticmethod
    def _is_filled(order: dict[str, Any]) -> bool:
        return str(order.get("status", "")).upper() == "FILLED" and _decimal(
            order.get("executedQty")
        ) > 0

    @staticmethod
    def _net_base_quantity(
        order: dict[str, Any],
        *,
        base_asset: str,
        step_size: Decimal,
    ) -> Decimal:
        executed = _decimal(order.get("executedQty"))
        base_commission = Decimal("0")
        fills = order.get("fills") or []
        if isinstance(fills, list):
            for fill in fills:
                if not isinstance(fill, dict):
                    continue
                if str(fill.get("commissionAsset", "")).upper() == base_asset.upper():
                    base_commission += _decimal(fill.get("commission"))
        net = executed - base_commission
        return _round_down(max(net, Decimal("0")), step_size)

    def _mark_buy_unknown_and_reconcile(
        self,
        *,
        client_order_id: str,
        symbol: str,
        cause: BaseException,
    ) -> dict[str, Any]:
        self.journal.transition(
            client_order_id,
            "BUY_UNKNOWN",
            details={"buy_unknown_type": type(cause).__name__},
        )
        try:
            order = self.client.query_order(
                symbol=symbol,
                client_order_id=client_order_id,
            )
        except Exception as query_exc:
            self.journal.transition(
                client_order_id,
                "BUY_UNKNOWN",
                details={"buy_reconcile_error": type(query_exc).__name__},
            )
            raise RecoveryRequired(
                "BUY_EXECUTION_UNKNOWN: reconcile clientOrderId before any retry"
            ) from cause

        if self._is_filled(order):
            self.journal.transition(
                client_order_id,
                "BUY_FILLED",
                details={
                    "buy_order_id": str(order.get("orderId", "")),
                    "executed_qty": str(order.get("executedQty", "")),
                    "reconciled": True,
                },
            )
            return order

        self.journal.transition(
            client_order_id,
            "BUY_UNKNOWN",
            details={"buy_reconcile_status": str(order.get("status", "UNKNOWN"))},
        )
        raise RecoveryRequired(
            "BUY_EXECUTION_NOT_RESOLVED: do not place another BUY"
        )

    def _emergency_flatten(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        client_order_id: str,
        flat_client_order_id: str,
        reason: str,
    ) -> None:
        self.journal.transition(
            client_order_id,
            "UNPROTECTED",
            details={
                "unprotected_reason": reason,
                "flatten_client_order_id": flat_client_order_id,
                "flatten_quantity": format(quantity, "f"),
            },
        )
        try:
            result = self.client.market_sell_quantity(
                symbol=symbol,
                quantity=quantity,
                client_order_id=flat_client_order_id,
            )
        except Exception as exc:
            self.journal.transition(
                client_order_id,
                "UNPROTECTED",
                details={
                    "flatten_error_type": type(exc).__name__,
                    "flatten_execution_unknown": self._is_uncertain_error(exc),
                },
            )
            raise RecoveryRequired(
                "EMERGENCY_FLATTEN_NOT_CONFIRMED: manual/account reconciliation required"
            ) from exc

        self.journal.transition(
            client_order_id,
            "FLATTENED",
            details={
                "flatten_order_id": str(result.get("orderId", "")),
                "flatten_status": str(result.get("status", "")),
            },
        )

    def execute(
        self,
        *,
        symbol: str,
        base_asset: str,
        signal_key: str,
        quote_size: Decimal,
        step_size: Decimal,
        min_qty: Decimal,
        min_notional: Decimal,
        take_profit_trigger: Decimal,
        stop_loss_trigger: Decimal,
    ) -> ProtectedExecutionResult:
        if self.journal.has_pending():
            raise RecoveryRequired(
                "LIVE_RECOVERY_REQUIRED: pending execution journal entries exist"
            )
        if quote_size <= 0 or step_size <= 0:
            raise ValueError("quote_size and step_size must be > 0")
        if min_qty < 0 or min_notional < 0:
            raise ValueError("minimum filters cannot be negative")
        if take_profit_trigger <= 0 or stop_loss_trigger <= 0:
            raise ValueError("protection triggers must be > 0")
        if take_profit_trigger <= stop_loss_trigger:
            raise ValueError("take_profit_trigger must be above stop_loss_trigger")

        symbol = symbol.upper().strip()
        base_asset = base_asset.upper().strip()
        ids = _ids(symbol, signal_key)

        existing = self.journal.get(ids["buy"])
        if existing is not None:
            if existing.stage in {"PROTECTED", "FLATTENED", "ABORTED"}:
                raise RuntimeError("execution_already_finalized_for_signal")
            raise RecoveryRequired("execution_already_pending_for_signal")

        self.journal.begin(
            client_order_id=ids["buy"],
            symbol=symbol,
            details={
                "signal_key": signal_key,
                "quote_size": format(quote_size, "f"),
                "oco_list_client_order_id": ids["oco"],
                "take_profit_trigger": format(take_profit_trigger, "f"),
                "stop_loss_trigger": format(stop_loss_trigger, "f"),
            },
        )

        try:
            buy = self.client.market_buy_quote(
                symbol=symbol,
                quote_order_qty=quote_size,
                client_order_id=ids["buy"],
            )
        except Exception as exc:
            if self._is_uncertain_error(exc):
                buy = self._mark_buy_unknown_and_reconcile(
                    client_order_id=ids["buy"],
                    symbol=symbol,
                    cause=exc,
                )
            else:
                self.journal.transition(
                    ids["buy"],
                    "ABORTED",
                    details={"buy_rejected_type": type(exc).__name__},
                )
                raise

        if not self._is_filled(buy):
            status = str(buy.get("status", "UNKNOWN")).upper()
            if status in {"REJECTED", "EXPIRED", "CANCELED"} and _decimal(
                buy.get("executedQty")
            ) <= 0:
                self.journal.transition(
                    ids["buy"],
                    "ABORTED",
                    details={"buy_status": status},
                )
                raise RuntimeError(f"market_buy_not_filled:{status}")
            self.journal.transition(
                ids["buy"],
                "BUY_UNKNOWN",
                details={"buy_status": status},
            )
            raise RecoveryRequired(
                "BUY_STATUS_NOT_FINAL: reconcile order before continuing"
            )

        if self.journal.get(ids["buy"]).stage != "BUY_FILLED":
            self.journal.transition(
                ids["buy"],
                "BUY_FILLED",
                details={
                    "buy_order_id": str(buy.get("orderId", "")),
                    "executed_qty": str(buy.get("executedQty", "")),
                },
            )

        quantity = self._net_base_quantity(
            buy,
            base_asset=base_asset,
            step_size=step_size,
        )
        protection_viable = (
            quantity >= min_qty
            and quantity * take_profit_trigger >= min_notional
            and quantity * stop_loss_trigger >= min_notional
        )
        if not protection_viable:
            self._emergency_flatten(
                symbol=symbol,
                quantity=quantity,
                client_order_id=ids["buy"],
                flat_client_order_id=ids["flat"],
                reason="post_fill_quantity_fails_protection_filters",
            )
            return ProtectedExecutionResult(
                symbol=symbol,
                client_order_id=ids["buy"],
                list_client_order_id=ids["oco"],
                protected_quantity=quantity,
                status="FLATTENED",
            )

        self.journal.transition(
            ids["buy"],
            "OCO_INTENT",
            details={"protected_quantity": format(quantity, "f")},
        )
        try:
            oco = self.client.place_oco_market_protection(
                symbol=symbol,
                quantity=quantity,
                take_profit_trigger=take_profit_trigger,
                stop_loss_trigger=stop_loss_trigger,
                list_client_order_id=ids["oco"],
                above_client_order_id=ids["tp"],
                below_client_order_id=ids["sl"],
            )
        except Exception as exc:
            if self._is_uncertain_error(exc):
                self.journal.transition(
                    ids["buy"],
                    "OCO_UNKNOWN",
                    details={"oco_unknown_type": type(exc).__name__},
                )
                try:
                    order_list = self.client.query_order_list(
                        list_client_order_id=ids["oco"]
                    )
                except Exception as query_exc:
                    self.journal.transition(
                        ids["buy"],
                        "OCO_UNKNOWN",
                        details={"oco_reconcile_error": type(query_exc).__name__},
                    )
                    raise RecoveryRequired(
                        "OCO_EXECUTION_UNKNOWN: do not flatten or place another OCO until reconciled"
                    ) from exc

                list_status = str(order_list.get("listOrderStatus", "")).upper()
                if list_status in {"EXECUTING", "ALL_DONE"}:
                    self.journal.transition(
                        ids["buy"],
                        "PROTECTED",
                        details={
                            "oco_reconciled": True,
                            "order_list_id": str(order_list.get("orderListId", "")),
                            "list_order_status": list_status,
                        },
                    )
                    return ProtectedExecutionResult(
                        symbol=symbol,
                        client_order_id=ids["buy"],
                        list_client_order_id=ids["oco"],
                        protected_quantity=quantity,
                        status="PROTECTED",
                    )

                raise RecoveryRequired(
                    "OCO_RECONCILIATION_NOT_FINAL: manual/account reconciliation required"
                ) from exc

            self._emergency_flatten(
                symbol=symbol,
                quantity=quantity,
                client_order_id=ids["buy"],
                flat_client_order_id=ids["flat"],
                reason=f"oco_rejected:{type(exc).__name__}",
            )
            return ProtectedExecutionResult(
                symbol=symbol,
                client_order_id=ids["buy"],
                list_client_order_id=ids["oco"],
                protected_quantity=quantity,
                status="FLATTENED",
            )

        self.journal.transition(
            ids["buy"],
            "PROTECTED",
            details={
                "order_list_id": str(oco.get("orderListId", "")),
                "list_order_status": str(oco.get("listOrderStatus", "")),
            },
        )
        return ProtectedExecutionResult(
            symbol=symbol,
            client_order_id=ids["buy"],
            list_client_order_id=ids["oco"],
            protected_quantity=quantity,
            status="PROTECTED",
        )
