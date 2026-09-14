from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy


class AdaptiveRegimeStrategy(IStrategy):
    """TST_ALLIGATOR_TMV_V1

    Causal Spot-long strategy built from the two source ideas we chose to keep:
    - Trend + Momentum + Volume + ATR discipline.
    - Williams Alligator + MACD + Parabolic SAR confirmation.

    The plotted forward shifts of the classic Alligator are deliberately NOT used,
    because they would introduce look-ahead bias in an automated strategy.
    """

    INTERFACE_VERSION = 3
    STRATEGY_ID = "TST_ALLIGATOR_TMV_V1"
    timeframe = "15m"
    can_short = False
    process_only_new_candles = True
    startup_candle_count = 220

    # Hard emergency floor. Normal exits are indicator-driven; execution-side
    # protection uses ATR sizing/levels in the Fusion/Hummingbot layer.
    stoploss = -0.03
    minimal_roi = {
        "0": 0.06,
        "240": 0.03,
        "720": 0.0,
    }

    trailing_stop = True
    trailing_stop_positive = 0.012
    trailing_stop_positive_offset = 0.022
    trailing_only_offset_is_reached = True

    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 2},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 24,
                "trade_limit": 2,
                "stop_duration_candles": 12,
                "only_per_pair": False,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 96,
                "trade_limit": 4,
                "stop_duration_candles": 24,
                "max_allowed_drawdown": 0.025,
            },
        ]

    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    @staticmethod
    def _smma(series, period: int):
        # Wilder-style smoothed moving average (RMA/SMMA), calculated causally.
        return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe["close"]

        # Williams Alligator, without forward plotting shifts.
        dataframe["jaw"] = self._smma(close, 13)
        dataframe["teeth"] = self._smma(close, 8)
        dataframe["lips"] = self._smma(close, 5)
        dataframe["alligator_spread"] = (dataframe["lips"] - dataframe["jaw"]) / close

        # Momentum confirmation.
        dataframe["ema12"] = ta.EMA(dataframe, timeperiod=12)
        dataframe["ema26"] = ta.EMA(dataframe, timeperiod=26)
        dataframe["macd"] = dataframe["ema12"] - dataframe["ema26"]
        dataframe["macd_signal"] = dataframe["macd"].ewm(span=9, adjust=False, min_periods=9).mean()
        dataframe["macd_hist"] = dataframe["macd"] - dataframe["macd_signal"]

        # Direction + volatility + broad trend.
        dataframe["sar"] = ta.SAR(dataframe, acceleration=0.02, maximum=0.2)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)

        # Current candle compared with the PRIOR 20-candle mean volume.
        dataframe["vol_mean20_prev"] = dataframe["volume"].rolling(20).mean().shift(1)
        dataframe["relvol"] = dataframe["volume"] / dataframe["vol_mean20_prev"]

        dataframe["alligator_bull"] = (
            (dataframe["lips"] > dataframe["teeth"])
            & (dataframe["teeth"] > dataframe["jaw"])
            & (dataframe["lips"] > dataframe["lips"].shift(1))
            & (dataframe["teeth"] > dataframe["teeth"].shift(1))
            & (dataframe["jaw"] > dataframe["jaw"].shift(1))
            & (dataframe["alligator_spread"] > dataframe["alligator_spread"].shift(1))
        )
        dataframe["macd_bull"] = (
            (dataframe["macd"] > dataframe["macd_signal"])
            & (dataframe["macd_hist"] > 0)
        )
        dataframe["sar_bull"] = dataframe["sar"] < dataframe["close"]
        dataframe["volume_confirmed"] = dataframe["relvol"] >= 1.20
        dataframe["trend_confirmed"] = dataframe["close"] > dataframe["ema200"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = None

        setup = (
            dataframe["alligator_bull"]
            & dataframe["macd_bull"]
            & dataframe["sar_bull"]
            & dataframe["volume_confirmed"]
            & dataframe["trend_confirmed"]
            & (dataframe["atr"] > 0)
            & (dataframe["volume"] > 0)
        )

        dataframe.loc[setup, ["enter_long", "enter_tag"]] = (1, "alligator_tmv")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = None

        alligator_failure = dataframe["lips"] < dataframe["teeth"]
        macd_failure = dataframe["macd"] < dataframe["macd_signal"]
        sar_flip = dataframe["sar"] > dataframe["close"]

        dataframe.loc[
            (alligator_failure | macd_failure | sar_flip) & (dataframe["volume"] > 0),
            ["exit_long", "exit_tag"],
        ] = (1, "trend_failure")
        return dataframe
