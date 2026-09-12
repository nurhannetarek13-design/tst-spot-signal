from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .binance_private import BinanceSignedSpotClient
from .config import Settings
from .protected_executor import ProtectedSpotExecutor
from .reconciliation import ReadOnlyExecutionReconciler


@dataclass
class LiveAdapterBundle:
    client: BinanceSignedSpotClient
    executor: ProtectedSpotExecutor
    reconciler: ReadOnlyExecutionReconciler

    def close(self) -> None:
        self.client.close()


def make_live_adapter(
    settings: Settings,
    *,
    journal: Any,
) -> LiveAdapterBundle | None:
    """Build the private Spot execution stack only for deliberately enabled Live.

    Construction performs no Binance request. Paper/Shadow always return None.
    The strategy engine's independent hard lock remains a separate gate, so
    creating this bundle alone can never place a real order.
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
