from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .binance_private import BinanceSignedSpotClient
from .config import Settings
from .protected_executor import ProtectedSpotExecutor
from .reconciliation import ReadOnlyExecutionReconciler


@dataclass(frozen=True)
class LiveAccountSnapshot:
    can_trade: bool
    free_quote: Decimal
    open_v2_order_lists: int

    def to_dict(self) -> dict[str, object]:
        return {
            "can_trade": self.can_trade,
            "free_quote": format(self.free_quote, "f"),
            "open_v2_order_lists": self.open_v2_order_lists,
        }


@dataclass
class LiveAdapterBundle:
    client: BinanceSignedSpotClient
    executor: ProtectedSpotExecutor
    reconciler: ReadOnlyExecutionReconciler

    def close(self) -> None:
        self.client.close()

    def inspect_account(self, *, quote_asset: str) -> LiveAccountSnapshot:
        """Read-only account guard used immediately before any private BUY.

        Only V2-owned order lists are counted (deterministic ``v2o`` prefix),
        so unrelated manual orders are never modified or silently adopted.
        """
        account = self.client.account_information(omit_zero_balances=True)
        can_trade = account.get("canTrade") is True
        quote = quote_asset.strip().upper()
        free_quote = Decimal("0")
        for row in account.get("balances", []) or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("asset", "")).upper() != quote:
                continue
            try:
                free_quote = Decimal(str(row.get("free", "0")))
            except Exception:
                free_quote = Decimal("0")
            break

        order_lists = self.client.open_order_lists()
        open_v2 = sum(
            1
            for row in order_lists
            if isinstance(row, dict)
            and str(row.get("listClientOrderId", "")).startswith("v2o")
            and str(row.get("listOrderStatus", "")).upper() in {"EXECUTING", "EXEC_STARTED"}
        )
        return LiveAccountSnapshot(
            can_trade=can_trade,
            free_quote=free_quote,
            open_v2_order_lists=open_v2,
        )


def make_live_adapter(
    settings: Settings,
    *,
    journal: Any,
) -> LiveAdapterBundle | None:
    """Build the private Spot execution stack only for deliberately enabled Live.

    Construction performs no Binance request. Paper/Shadow always return None.
    Runtime readiness and explicit engine authorization remain independent gates,
    so creating this bundle alone can never place a real order.
    """

    if settings.mode != "live":
        return None
    if not settings.private_adapter_enabled:
        return None
    if not settings.private_credentials_present:
        raise RuntimeError("private_adapter_credentials_missing")

    client = BinanceSignedSpotClient(
        api_key=settings.binance_api_key,
        api_secret=settings.binance_api_secret,
    )
    executor = ProtectedSpotExecutor(client=client, journal=journal)
    reconciler = ReadOnlyExecutionReconciler(client=client, journal=journal)
    return LiveAdapterBundle(
        client=client,
        executor=executor,
        reconciler=reconciler,
    )