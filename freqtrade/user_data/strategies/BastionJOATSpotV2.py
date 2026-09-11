from __future__ import annotations

from datetime import datetime
import pandas as pd
from freqtrade.persistence import Trade
from freqtrade.strategy import stoploss_from_absolute

from BastionJOATSpotV1 import BastionJOATSpotV1


class BastionJOATSpotV2(BastionJOATSpotV1):
    """JOAT Spot candidate with Pine-like ATR SL / 2R TP / ATR trailing.

    Still RESEARCH_ONLY. No live authorization and no position adjustment.
    """

    use_custom_stoploss = True
    stoploss = -0.10  # fail-safe floor only; custom stop is the canonical path.

    def _signal_and_current_candle(self, pair: str, trade: Trade, current_time: datetime):
        if self.dp is None:
            return None, None
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or df.empty:
            return None, None
        dates = pd.to_datetime(df["date"], utc=True)
        open_ts = pd.Timestamp(trade.open_date_utc)
        if open_ts.tzinfo is None:
            open_ts = open_ts.tz_localize("UTC")
        else:
            open_ts = open_ts.tz_convert("UTC")
        now_ts = pd.Timestamp(current_time)
        if now_ts.tzinfo is None:
            now_ts = now_ts.tz_localize("UTC")
        else:
            now_ts = now_ts.tz_convert("UTC")
        before = df.loc[dates < open_ts]
        upto = df.loc[dates <= now_ts]
        if before.empty or upto.empty:
            return None, None
        return before.iloc[-1], upto.iloc[-1]

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float | None:
        signal, candle = self._signal_and_current_candle(pair, trade, current_time)
        if signal is None or candle is None:
            return None
        signal_close = float(signal["close"])
        signal_atr = float(signal["atr"])
        current_close = float(candle["close"])
        current_atr = float(candle["atr"])
        if signal_close <= 0 or signal_atr <= 0 or current_atr <= 0:
            return None

        # Pine initializes stop from the signal-bar close.
        stop_price = signal_close - self.sl_atr_mult * signal_atr

        # Pine profitR uses activeEntryPrice=signal close and current slDist.
        current_sl_dist = self.sl_atr_mult * current_atr
        profit_r = (current_close - signal_close) / current_sl_dist if current_sl_dist > 0 else 0.0
        if profit_r >= 1.0:
            # Pine trail: close - ATR*1.0, ratcheted only upward. Freqtrade's
            # custom stoploss itself enforces the no-widening ratchet.
            stop_price = max(stop_price, current_close - current_atr)

        if stop_price >= current_rate:
            return 0.001
        return stoploss_from_absolute(
            stop_price,
            current_rate=current_rate,
            is_short=False,
            leverage=1.0,
        )

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | bool | None:
        signal, candle = self._signal_and_current_candle(pair, trade, current_time)
        if signal is None or candle is None:
            return None
        signal_close = float(signal["close"])
        signal_atr = float(signal["atr"])
        if signal_close <= 0 or signal_atr <= 0:
            return None

        # Pine longTP = signal close + (ATR*1.5)*2.0.
        target = signal_close + (self.sl_atr_mult * signal_atr * self.rr_ratio)
        if current_rate >= target:
            return "joat_2r_tp"

        # Pine force-close default: weekdays at/after 15:45 America/New_York.
        ny = pd.Timestamp(current_time)
        if ny.tzinfo is None:
            ny = ny.tz_localize("UTC")
        ny = ny.tz_convert("America/New_York")
        if ny.dayofweek <= 4 and (ny.hour > 15 or (ny.hour == 15 and ny.minute >= 45)):
            return "joat_eod_close"
        return None
