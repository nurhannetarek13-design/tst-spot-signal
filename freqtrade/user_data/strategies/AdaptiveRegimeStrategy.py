from datetime import datetime
from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from technical import qtpylib


class AdaptiveRegimeStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "15m"
    can_short = False
    process_only_new_candles = True
    startup_candle_count = 220

    # Conservative baseline for a very small Spot account.
    stoploss = -0.022
    minimal_roi = {
        "0": 0.03,
        "180": 0.018,
        "480": 0.008,
        "960": 0.0,
    }

    trailing_stop = True
    trailing_stop_positive = 0.008
    trailing_stop_positive_offset = 0.014
    trailing_only_offset_is_reached = True

    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 2},
            {"method": "StoplossGuard", "lookback_period_candles": 24, "trade_limit": 2, "stop_duration_candles": 12, "only_per_pair": False},
            {"method": "MaxDrawdown", "lookback_period_candles": 96, "trade_limit": 4, "stop_duration_candles": 24, "max_allowed_drawdown": 0.025},
        ]

    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)

        bb = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe["bb_lower"] = bb["lower"]
        dataframe["bb_mid"] = bb["mid"]
        dataframe["bb_upper"] = bb["upper"]
        dataframe["bb_width"] = (dataframe["bb_upper"] - dataframe["bb_lower"]) / dataframe["bb_mid"]

        dataframe["vol_med24"] = dataframe["volume"].rolling(24).median()
        dataframe["relvol"] = dataframe["volume"] / dataframe["vol_med24"]
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["atr_pct_med48"] = dataframe["atr_pct"].rolling(48).median()
        dataframe["hh20"] = dataframe["high"].rolling(20).max().shift(1)
        dataframe["ll20"] = dataframe["low"].rolling(20).min().shift(1)

        # Regimes. These are explicit and mutually interpretable, not ML guesses.
        dataframe["regime_trend"] = (
            (dataframe["close"] > dataframe["ema200"])
            & (dataframe["ema20"] > dataframe["ema50"])
            & (dataframe["adx"] >= 22)
        )
        dataframe["regime_range"] = (
            (dataframe["adx"] < 19)
            & (dataframe["bb_width"] < dataframe["bb_width"].rolling(48).median() * 1.15)
        )
        dataframe["regime_volatility"] = (
            (dataframe["atr_pct"] > dataframe["atr_pct_med48"] * 1.35)
            & (dataframe["relvol"] >= 1.5)
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = None

        trend_breakout = (
            dataframe["regime_trend"]
            & (dataframe["close"] > dataframe["hh20"])
            & (dataframe["relvol"] >= 1.25)
            & dataframe["rsi"].between(53, 70)
            & (dataframe["volume"] > 0)
        )

        trend_pullback = (
            dataframe["regime_trend"]
            & (dataframe["low"] <= dataframe["ema20"] * 1.004)
            & (dataframe["close"] > dataframe["ema20"])
            & (dataframe["close"] > dataframe["open"])
            & dataframe["rsi"].between(48, 65)
            & (dataframe["volume"] > 0)
        )

        mean_reversion = (
            dataframe["regime_range"]
            & (dataframe["close"] < dataframe["bb_lower"])
            & (dataframe["rsi"] < 32)
            & (dataframe["relvol"] >= 0.8)
            & (dataframe["volume"] > 0)
        )

        volatility_momentum = (
            dataframe["regime_volatility"]
            & (dataframe["close"] > dataframe["hh20"])
            & (dataframe["close"] > dataframe["ema50"])
            & dataframe["rsi"].between(55, 73)
            & (dataframe["volume"] > 0)
        )

        dataframe.loc[trend_breakout, ["enter_long", "enter_tag"]] = (1, "trend_breakout")
        dataframe.loc[trend_pullback & ~trend_breakout, ["enter_long", "enter_tag"]] = (1, "trend_pullback")
        dataframe.loc[mean_reversion & ~(trend_breakout | trend_pullback), ["enter_long", "enter_tag"]] = (1, "mean_reversion")
        dataframe.loc[volatility_momentum, ["enter_long", "enter_tag"]] = (1, "volatility_momentum")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = None

        trend_failure = (
            (dataframe["close"] < dataframe["ema20"])
            & (dataframe["ema20"] < dataframe["ema50"])
            & (dataframe["volume"] > 0)
        )
        range_reversion_done = (
            (dataframe["close"] >= dataframe["bb_mid"])
            & (dataframe["rsi"] >= 50)
            & (dataframe["volume"] > 0)
        )
        volatility_failure = (
            (dataframe["close"] < dataframe["ema20"])
            & (dataframe["rsi"] < 45)
            & (dataframe["volume"] > 0)
        )

        dataframe.loc[trend_failure, ["exit_long", "exit_tag"]] = (1, "trend_failure")
        dataframe.loc[range_reversion_done, ["exit_long", "exit_tag"]] = (1, "range_reversion_done")
        dataframe.loc[volatility_failure, ["exit_long", "exit_tag"]] = (1, "volatility_failure")
        return dataframe
