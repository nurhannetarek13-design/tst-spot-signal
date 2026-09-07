from __future__ import annotations

from datetime import datetime, timezone

from freqtrade.persistence import Trade
from NostalgiaForInfinityX7 import NostalgiaForInfinityX7


class NFIProtectedX7(NostalgiaForInfinityX7):
    """NFI X7 with hard account-level guardrails and fast-trade exits.

    Upstream entry logic is preserved. Risk and holding time are constrained here.
    """

    stoploss = -0.08
    position_adjustment_enable = False
    max_entry_position_adjustment = 0

    DAILY_LOSS_LIMIT_USDT = 2.0
    MAX_HOLD_MINUTES = 90
    FAST_TP = 0.012
    SOFT_PROFIT = 0.002
    SOFT_PROFIT_AFTER_MINUTES = 45

    @staticmethod
    def _realized_pnl_today(current_time: datetime) -> float:
        now = current_time.astimezone(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        trades = Trade.get_trades_proxy(is_open=False, close_date=day_start)
        return float(sum((t.close_profit_abs or 0.0) for t in trades))

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> bool:
        if self._realized_pnl_today(current_time) <= -self.DAILY_LOSS_LIMIT_USDT:
            return False

        return bool(
            super().confirm_trade_entry(
                pair=pair,
                order_type=order_type,
                amount=amount,
                rate=rate,
                time_in_force=time_in_force,
                current_time=current_time,
                entry_tag=entry_tag,
                side=side,
                **kwargs,
            )
        )

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ):
        age_minutes = (current_time - trade.open_date_utc).total_seconds() / 60.0

        if current_profit >= self.FAST_TP:
            return "fast_tp_1p2"

        if age_minutes >= self.SOFT_PROFIT_AFTER_MINUTES and current_profit >= self.SOFT_PROFIT:
            return "fast_soft_profit"

        if age_minutes >= self.MAX_HOLD_MINUTES:
            return "fast_time_cap_90m"

        parent_exit = super().custom_exit(
            pair=pair,
            trade=trade,
            current_time=current_time,
            current_rate=current_rate,
            current_profit=current_profit,
            **kwargs,
        )
        return parent_exit
