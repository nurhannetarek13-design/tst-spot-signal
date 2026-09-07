from __future__ import annotations

from datetime import datetime, timezone

from freqtrade.persistence import Trade
from NostalgiaForInfinityX7 import NostalgiaForInfinityX7


class NFIProtectedX7(NostalgiaForInfinityX7):
    """NFI X7 signal-only scanner with fast-trade and hard-risk filters."""

    stoploss = -0.08
    position_adjustment_enable = False
    max_entry_position_adjustment = 0

    DAILY_LOSS_LIMIT_USDT = 2.0
    MAX_HOLD_MINUTES = 60
    FAST_TP = 0.015
    SOFT_PROFIT = 0.004
    SOFT_PROFIT_AFTER_MINUTES = 30
    ALERT_COOLDOWN_MINUTES = 15

    # A BUY alert is allowed only when recent realized movement shows enough
    # capacity to plausibly cover the requested 1.5% target within ~1 hour.
    MIN_1H_RANGE = 0.018
    MIN_15M_MOMENTUM = 0.0015

    _last_alert_by_pair: dict[str, datetime] = {}

    @staticmethod
    def _realized_pnl_today(current_time: datetime) -> float:
        now = current_time.astimezone(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        trades = Trade.get_trades_proxy(is_open=False, close_date=day_start)
        return float(sum((t.close_profit_abs or 0.0) for t in trades))

    def _alert_allowed(self, pair: str, current_time: datetime) -> bool:
        prev = self._last_alert_by_pair.get(pair)
        if prev is None:
            return True
        return (current_time - prev).total_seconds() >= self.ALERT_COOLDOWN_MINUTES * 60

    def _fast_trade_ok(self, pair: str) -> bool:
        """Require enough recent movement and positive short momentum.

        Runtime timeframe is 5m, so 12 candles ~= 1h and 3 candles ~= 15m.
        This does not guarantee the target; it rejects setups where the recent
        market has not even demonstrated enough movement capacity.
        """
        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is None or len(dataframe) < 13:
                return False
            d = dataframe.iloc[-12:]
            last = float(d['close'].iloc[-1])
            if last <= 0:
                return False
            one_hour_range = (float(d['high'].max()) - float(d['low'].min())) / last
            close_15m_ago = float(dataframe['close'].iloc[-4])
            momentum_15m = (last / close_15m_ago) - 1.0 if close_15m_ago > 0 else -1.0
            return one_hour_range >= self.MIN_1H_RANGE and momentum_15m >= self.MIN_15M_MOMENTUM
        except Exception as exc:
            print(f'[fast-filter] failed for {pair}: {exc}')
            return False

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

        parent_ok = bool(
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
        if not parent_ok or not self._fast_trade_ok(pair):
            return False

        if self._alert_allowed(pair, current_time):
            try:
                from telegram_signal_bridge import send_opportunity
                stake_usdt = float(amount) * float(rate)
                tp = float(rate) * (1.0 + self.FAST_TP)
                sl = float(rate) * (1.0 + self.stoploss)
                send_opportunity(
                    pair=pair,
                    stake_usdt=stake_usdt,
                    entry=float(rate),
                    tp=tp,
                    sl=sl,
                    tag=(entry_tag or 'NFIProtectedX7') + '|FAST<=60M',
                )
                self._last_alert_by_pair[pair] = current_time
            except Exception as exc:
                print(f'[telegram-signal] failed for {pair}: {exc}')

        # Never auto-place the trade.
        return False

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
            return 'fast_tp_1p5'
        if age_minutes >= self.SOFT_PROFIT_AFTER_MINUTES and current_profit >= self.SOFT_PROFIT:
            return 'fast_soft_profit_30m'
        if age_minutes >= self.MAX_HOLD_MINUTES:
            return 'fast_time_cap_60m'
        return super().custom_exit(
            pair=pair,
            trade=trade,
            current_time=current_time,
            current_rate=current_rate,
            current_profit=current_profit,
            **kwargs,
        )
