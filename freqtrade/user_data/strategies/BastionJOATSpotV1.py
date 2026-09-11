from __future__ import annotations

from pandas import DataFrame
import numpy as np
import pandas as pd
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from technical import qtpylib


class BastionJOATSpotV1(IStrategy):
    """Research-only Spot/Long port of Bastion Execution Protocol [JOAT].

    Pine source supplied by the user (MPL-2.0). Entry logic is intentionally
    kept close to the original defaults. Pine short execution and Pine equity
    sizing are deliberately not ported because this bot is Binance Spot only
    and stake/risk is owned by the execution layer.

    IMPORTANT: NOT LIVE READY. Exit parity is still under validation.
    """

    INTERFACE_VERSION = 3
    timeframe = "15m"
    can_short = False
    process_only_new_candles = True
    startup_candle_count = 240

    # Fail-safe only. Canonical JOAT uses ATR-derived initial SL and 2R target.
    stoploss = -0.10
    minimal_roi = {"0": 100.0}
    use_exit_signal = False
    trailing_stop = False

    # Frozen Pine defaults.
    atr_len = 14
    sl_atr_mult = 1.5
    rr_ratio = 2.0
    vwap_len = 20
    slope_thr = 0.12
    bb_len = 20
    bb_mult = 2.0
    swing_len = 5
    disp_body = 0.70
    disp_mult = 1.8
    rsi_len = 14
    rsi_bull = 55.0
    smi_look = 13
    smi_sm1 = 25
    smi_sm2 = 2
    pattern_vol_mult = 1.3
    cvd_look = 10

    @staticmethod
    def _rolling_percentrank(series: pd.Series, window: int) -> pd.Series:
        def rank_last(x: np.ndarray) -> float:
            if len(x) < window or not np.isfinite(x[-1]):
                return np.nan
            v = x[-1]
            valid = x[np.isfinite(x)]
            if not len(valid):
                return np.nan
            return 100.0 * float(np.sum(valid <= v) - 1) / max(1, len(valid) - 1)
        return series.rolling(window, min_periods=window).apply(rank_last, raw=True)

    @staticmethod
    def _session_vwap(dataframe: DataFrame) -> pd.Series:
        dt = pd.to_datetime(dataframe["date"], utc=True)
        session = dt.dt.floor("D")
        tp = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3.0
        pv = tp * dataframe["volume"]
        return pv.groupby(session).cumsum() / dataframe["volume"].groupby(session).cumsum().replace(0, np.nan)

    def _structure_trend(self, dataframe: DataFrame) -> pd.Series:
        n = self.swing_len
        # Pine pivots are only known n bars after the candidate pivot.
        pivot_high = dataframe["high"].shift(n).where(
            dataframe["high"].shift(n).eq(dataframe["high"].rolling(2 * n + 1).max())
        )
        pivot_low = dataframe["low"].shift(n).where(
            dataframe["low"].shift(n).eq(dataframe["low"].rolling(2 * n + 1).min())
        )
        last_sh = pivot_high.ffill()
        last_sl = pivot_low.ffill()
        state = 0
        out = []
        for close, sh, sl in zip(dataframe["close"].to_numpy(), last_sh.to_numpy(), last_sl.to_numpy()):
            if np.isfinite(sh) and close > sh and state <= 0:
                state = 1
            if np.isfinite(sl) and close < sl and state >= 0:
                state = -1
            out.append(state)
        return pd.Series(out, index=dataframe.index, dtype="int64")

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=self.atr_len)
        dataframe["sma20"] = ta.SMA(dataframe, timeperiod=20)
        dataframe["sma50"] = ta.SMA(dataframe, timeperiod=50)
        dataframe["sma200"] = ta.SMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=self.rsi_len)

        dataframe["vwap"] = self._session_vwap(dataframe)
        vwap_delta = dataframe["vwap"] - dataframe["vwap"].shift(self.vwap_len).fillna(dataframe["vwap"])
        dataframe["norm_slope"] = vwap_delta / (dataframe["atr"] * np.sqrt(self.vwap_len)).replace(0, np.nan)
        dataframe["slope_up"] = dataframe["norm_slope"] > self.slope_thr
        dataframe["slope_dn"] = dataframe["norm_slope"] < -self.slope_thr
        dataframe["sma_bull"] = (dataframe["sma20"] > dataframe["sma50"]) & (dataframe["sma50"] > dataframe["sma200"])
        dataframe["sma_bear"] = (dataframe["sma20"] < dataframe["sma50"]) & (dataframe["sma50"] < dataframe["sma200"])

        bb = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=self.bb_len, stds=self.bb_mult)
        dataframe["bb_mid"] = bb["mid"]
        dataframe["bb_up"] = bb["upper"]
        dataframe["bb_dn"] = bb["lower"]
        dataframe["bb_width_pct"] = (dataframe["bb_up"] - dataframe["bb_dn"]) / dataframe["bb_mid"].replace(0, np.nan) * 100.0
        dataframe["bb_pct_rank"] = self._rolling_percentrank(dataframe["bb_width_pct"], 120)
        dataframe["is_squeeze"] = dataframe["bb_pct_rank"] < 10.0

        # Pine priority: squeeze > strong bull/bear > partial bull/bear > range.
        regime = np.zeros(len(dataframe), dtype=np.int8)
        bull = dataframe["sma_bull"] & dataframe["slope_up"]
        bear = dataframe["sma_bear"] & dataframe["slope_dn"]
        partial_bull = dataframe["slope_up"] | dataframe["sma_bull"]
        partial_bear = dataframe["slope_dn"] | dataframe["sma_bear"]
        regime[np.asarray(partial_bull.fillna(False))] = 1
        regime[np.asarray(partial_bear.fillna(False))] = -1
        regime[np.asarray(bull.fillna(False))] = 1
        regime[np.asarray(bear.fillna(False))] = -1
        regime[np.asarray(dataframe["is_squeeze"].fillna(False))] = 0
        dataframe["regime_bull"] = regime == 1
        dataframe["regime_bear"] = regime == -1
        dataframe["regime_trending"] = regime != 0

        dataframe["struct_trend"] = self._structure_trend(dataframe)

        body = (dataframe["close"] - dataframe["open"]).abs()
        candle_range = dataframe["high"] - dataframe["low"]
        avg_body = body.rolling(20).mean()
        body_ratio = body / candle_range.replace(0, np.nan)
        displacement = (body_ratio >= self.disp_body) & (body >= avg_body * self.disp_mult) & (candle_range > 0)
        dataframe["bull_disp"] = displacement & (dataframe["close"] > dataframe["open"])

        high_h = dataframe["high"].rolling(self.smi_look).max()
        low_l = dataframe["low"].rolling(self.smi_look).min()
        dist = dataframe["close"] - (high_h + low_l) / 2.0
        rng = high_h - low_l
        sm2n = dist.ewm(span=self.smi_sm1, adjust=False).mean().ewm(span=self.smi_sm2, adjust=False).mean()
        sm2d = rng.ewm(span=self.smi_sm1, adjust=False).mean().ewm(span=self.smi_sm2, adjust=False).mean() * 0.5
        dataframe["smi"] = 100.0 * sm2n / sm2d.replace(0, np.nan)
        dataframe["mom_bull"] = (dataframe["rsi"] > self.rsi_bull) & (dataframe["smi"] > 0)

        # Exact Pine proxy CVD: candle-location estimate, not exchange taker flow.
        denom = (dataframe["high"] - dataframe["low"]).replace(0, np.nan)
        buy_vol = np.where(
            dataframe["close"] >= dataframe["open"],
            dataframe["volume"],
            dataframe["volume"] * (dataframe["close"] - dataframe["low"]) / denom,
        )
        dataframe["cvd"] = pd.Series(buy_vol, index=dataframe.index).sub(dataframe["volume"] - buy_vol).fillna(0).cumsum()
        dataframe["cvd_ma"] = dataframe["cvd"].rolling(self.cvd_look).mean()
        dataframe["cvd_bull"] = dataframe["cvd"] > dataframe["cvd_ma"]

        avg_vol = dataframe["volume"].rolling(20).mean()
        high_vol = dataframe["volume"] > avg_vol * self.pattern_vol_mult
        candle_body = body
        lower_wick = np.minimum(dataframe["open"], dataframe["close"]) - dataframe["low"]
        upper_wick = dataframe["high"] - np.maximum(dataframe["open"], dataframe["close"])
        bull_engulf = (
            (dataframe["close"] > dataframe["open"])
            & (dataframe["close"].shift(1) < dataframe["open"].shift(1))
            & (dataframe["close"] > dataframe["open"].shift(1))
            & (dataframe["open"] < dataframe["close"].shift(1))
            & (candle_body > (dataframe["close"].shift(1) - dataframe["open"].shift(1)).abs())
            & high_vol
        )
        bull_pin = (lower_wick > candle_body * 2.0) & (upper_wick < candle_body * 0.5) & (candle_range > 0)
        dataframe["bull_pattern"] = bull_engulf | bull_pin

        # Original NY timezone/session/day filter. Pine disables weekends.
        ny = pd.to_datetime(dataframe["date"], utc=True).dt.tz_convert("America/New_York")
        mins = ny.dt.hour * 60 + ny.dt.minute
        in_nykz = mins.between(7 * 60, 10 * 60 - 1)
        in_lonkz = mins.between(2 * 60, 5 * 60 - 1)
        in_ny = mins.between(9 * 60 + 30, 16 * 60 - 1)
        in_lon = mins.between(3 * 60, 9 * 60 + 30 - 1)
        dataframe["session_ok"] = (in_nykz | in_lonkz | in_ny | in_lon) & (ny.dt.dayofweek <= 4)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = None

        trade_allowed = dataframe["session_ok"] & ~dataframe["is_squeeze"] & dataframe["regime_trending"]
        long_base = (
            dataframe["regime_bull"]
            & (dataframe["struct_trend"] == 1)
            & dataframe["mom_bull"]
            & dataframe["cvd_bull"]
            & trade_allowed
        )
        confirmation = dataframe["bull_disp"] | dataframe["bull_pattern"] | (
            (dataframe["close"] > dataframe["sma20"]) & (dataframe["close"] > dataframe["vwap"])
        )
        enter = long_base & confirmation & (dataframe["volume"] > 0)
        dataframe.loc[enter, ["enter_long", "enter_tag"]] = (1, "JOAT_LONG")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit parity is handled by the canonical validator first; this wrapper
        # intentionally emits no discretionary exit signal and is NOT live-ready.
        dataframe["exit_long"] = 0
        return dataframe
