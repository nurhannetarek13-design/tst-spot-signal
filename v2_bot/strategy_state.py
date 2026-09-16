from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .strategy_pool import ALL_SPECS


@dataclass(frozen=True)
class ExitProfile:
    strategy_id: str
    take_profit_pct: float
    stop_loss_pct: float


class StrategyAwareStateProxy:
    """Wrap the existing durable state without changing its schema.

    The base engine's paper-open API only accepts TP/SL percentages. The
    multi-strategy runtime prepares a strategy-specific profile immediately
    before the open. The resulting position permanently encodes the profile in
    its take-profit and stop-loss prices, so strategy identity can be recovered
    after a process/deployment restart without a new database column.
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self._pending: dict[str, ExitProfile] = {}

    def __getattr__(self, name: str):
        return getattr(self.store, name)

    def prepare_profile(
        self,
        *,
        symbol: str,
        strategy_id: str,
        take_profit_pct: float,
        stop_loss_pct: float,
    ) -> None:
        self._pending[str(symbol)] = ExitProfile(
            strategy_id=str(strategy_id),
            take_profit_pct=float(take_profit_pct),
            stop_loss_pct=float(stop_loss_pct),
        )

    def clear_profile(self, symbol: str) -> None:
        self._pending.pop(str(symbol), None)

    def open_position(
        self,
        *,
        symbol: str,
        entry_price: float,
        quote_size: float,
        take_profit_pct: float,
        stop_loss_pct: float,
    ):
        profile = self._pending.pop(str(symbol), None)
        if profile is not None:
            take_profit_pct = profile.take_profit_pct
            stop_loss_pct = profile.stop_loss_pct
        return self.store.open_position(
            symbol=symbol,
            entry_price=entry_price,
            quote_size=quote_size,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
        )

    @staticmethod
    def strategy_for_position(position: Any) -> str | None:
        entry = float(position.entry_price)
        if entry <= 0:
            return None
        tp_pct = float(position.take_profit) / entry - 1.0
        sl_pct = 1.0 - float(position.stop_loss) / entry
        matches = [
            spec.strategy_id
            for spec in ALL_SPECS
            if abs(tp_pct - spec.take_profit_pct) <= 1e-8
            and abs(sl_pct - spec.stop_loss_pct) <= 1e-8
        ]
        return matches[0] if len(matches) == 1 else None

    def close_position(self, position, *, exit_price: float, reason: str, fee_rate: float) -> float:
        strategy_id = self.strategy_for_position(position)
        stored_reason = f"{strategy_id}:{reason}" if strategy_id else reason
        return self.store.close_position(
            position,
            exit_price=exit_price,
            reason=stored_reason,
            fee_rate=fee_rate,
        )
