from __future__ import annotations

from datetime import datetime, timezone

from freqtrade.persistence import Trade
from NostalgiaForInfinityX7 import NostalgiaForInfinityX7


class NFIProtectedX7(NostalgiaForInfinityX7):
    """NFI X7 final-confirmation layer for 30–60 minute signal-only trades.

    Stage 1 PRE-ALERT is produced by the lightweight market watcher.
    Stage 2 BUY is emitted here only after a real NFI entry is present and
    the market still has enough short-term movement for a fast trade.
    """

    # The bot never auto-buys, but this value is also used to build the manual
    # BUY preview. -8% was inappropriate for a 30–60m target, so fast-trade
    # risk is capped near the size of the intended move.
    stoploss = -0.012
    position_adjustment_enable = False
    max_entry_position_adjustment = 0

    DAILY_LOSS_LIMIT_USDT = 2.0
    MAX_HOLD_MINUTES = 60
    FAST_TP = 0.015
    FAST_SL = 0.012
    SOFT_PROFIT = 0.004
    SOFT_PROFIT_AFTER_MINUTES = 30
    ALERT_COOLDOWN_MINUTES = 15

    # NFI itself is the high-quality confirmation. These are now an anti-dead
    # market gate rather than a second ultra-strict strategy stacked on top.
    MIN_1H_RANGE = 0.012       # 1.2% demonstrated movement capacity
    MIN_15M_MOMENTUM = 0.0005 # +0.05% positive short momentum

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

    def _fast_trade_metrics(self, pair: str) -> tuple[bool, float, float]:
        """Return (allowed, 1h range, 15m momentum) from analyzed 5m data."""
        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is None or len(dataframe) < 13:
                return False, 0.0, 0.0
            d = dataframe.iloc[-12:]
            last = float(d['close'].iloc[-1])
            if last <= 0:
                return False, 0.0, 0.0
            one_hour_range = (float(d['high'].max()) - float(d['low'].min())) / last
            close_15m_ago = float(dataframe['close'].iloc[-4])
            momentum_15m = (last / close_15m_ago) - 1.0 if close_15m_ago > 0 else -1.0
            allowed = one_hour_range >= self.MIN_1H_RANGE and momentum_15m >= self.MIN_15M_MOMENTUM
            return allowed, one_hour_range, momentum_15m
        except Exception as exc:
            print(f'[fast-filter] failed for {pair}: {type(exc).__name__}: {exc}')
            return False, 0.0, 0.0

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
            print(f'[buy-gate] {pair} blocked: daily loss cap reached')
            return False

        parent_result = super().confirm_trade_entry(
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
        # IStrategy implementations may return None when they do not veto.
        parent_ok = parent_result is not False
        if not parent_ok:
            print(f'[buy-gate] {pair} blocked by NFI confirm_trade_entry')
            return False

        fast_ok, one_hour_range, momentum_15m = self._fast_trade_metrics(pair)
        print(
            f'[nfi-entry] {pair} range1h={one_hour_range*100:.2f}% '
            f'mom15m={momentum_15m*100:+.2f}% fast_ok={fast_ok}'
        )
        if not fast_ok:
            return False

        if self._alert_allowed(pair, current_time):
            try:
                from telegram_signal_bridge import send_opportunity
                stake_usdt = float(amount) * float(rate)
                tp = float(rate) * (1.0 + self.FAST_TP)
                sl = float(rate) * (1.0 - self.FAST_SL)
                send_opportunity(
                    pair=pair,
                    stake_usdt=stake_usdt,
                    entry=float(rate),
                    tp=tp,
                    sl=sl,
                    tag=(entry_tag or 'NFIProtectedX7') + '|NFI_CONFIRMED|FAST<=60M',
                )
                self._last_alert_by_pair[pair] = current_time
                print(f'[telegram-signal] BUY sent for {pair}')
            except Exception as exc:
                print(f'[telegram-signal] failed for {pair}: {type(exc).__name__}: {exc}')

        # Signal-only: never allow Freqtrade to place a real or simulated entry.
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
