from __future__ import annotations

from datetime import datetime, timezone

from freqtrade.persistence import Trade
from NostalgiaForInfinityX7 import NostalgiaForInfinityX7


class NFIProtectedX7(NostalgiaForInfinityX7):
    """NFI X7 with hard account-level guardrails for small Spot accounts.

    This wrapper intentionally leaves NFI's entry/exit signal logic untouched while
    constraining risk at execution time.
    """

    # Hard per-trade loss cap. With a 5.5 USDT stake this is about 0.44 USDT
    # before fees/slippage, far below the account's 2 USDT daily loss ceiling.
    stoploss = -0.08

    # NFI can otherwise add repeatedly to losing positions. Disable DCA completely.
    position_adjustment_enable = False
    max_entry_position_adjustment = 0

    # Absolute realized-loss ceiling for each UTC day.
    DAILY_LOSS_LIMIT_USDT = 2.0

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
        # Fail closed after the realized daily loss ceiling is reached.
        if self._realized_pnl_today(current_time) <= -self.DAILY_LOSS_LIMIT_USDT:
            return False

        # Preserve any entry confirmation logic implemented by upstream NFI.
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
