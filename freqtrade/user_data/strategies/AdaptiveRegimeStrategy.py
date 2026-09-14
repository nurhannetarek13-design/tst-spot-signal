from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy


class AdaptiveRegimeStrategy(IStrategy):
    """TST_ALLIGATOR_SMC_V2

    Causal Spot-long strategy combining:
    - Williams Alligator + MACD + Parabolic SAR.
    - Trend + momentum + relative-volume confirmation.
    - Deterministic market-structure context: prior demand zone, liquidity sweep,
      bullish market shift/BOS, location discipline and minimum 2R room.

    The classic forward plotting shifts of Williams Alligator are NOT used.
    """

    INTERFACE_VERSION = 3
    STRATEGY_ID = "TST_ALLIGATOR_SMC_V2"
    timeframe = "15m"
    can_short = False
    process_only_new_candles = True
    startup_candle_count = 360

    # Emergency validator floor. The execution controller derives the normal stop
    # from structural invalidation below the active demand zone.
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
        return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    @staticmethod
    def _add_structure_context(dataframe: DataFrame) -> DataFrame:
        """Build a fully causal SMC-style context on completed bars.

        Rules:
        - Demand zone: previous bearish pivot candle before a bullish displacement.
        - Liquidity sweep: take prior 12-bar low, reclaim it, and interact with demand.
        - Market shift/BOS: close above prior 12-bar high after the sweep.
        - Location: stay within 3 ATR of the active demand-zone top.
        - Room: reject if known overhead 48-bar liquidity prevents >= 2R.
        """
        n = len(dataframe)
        if n == 0:
            return dataframe

        opens = dataframe["open"].astype(float).to_numpy()
        highs = dataframe["high"].astype(float).to_numpy()
        lows = dataframe["low"].astype(float).to_numpy()
        closes = dataframe["close"].astype(float).to_numpy()
        atrs = dataframe["atr"].astype(float).to_numpy()
        relvols = dataframe["relvol"].astype(float).to_numpy()

        demand_low_out = [float("nan")] * n
        demand_high_out = [float("nan")] * n
        sweep_low_out = [float("nan")] * n
        liquidity_sweep_out = [False] * n
        market_shift_out = [False] * n
        demand_location_out = [False] * n
        room_2r_out = [False] * n
        structure_risk_pct_out = [float("nan")] * n

        demand_low = None
        demand_high = None
        last_sweep_idx = -10_000
        last_sweep_low = None
        last_shift_idx = -10_000

        for i in range(n):
            atr_now = atrs[i]
            if demand_low is not None and closes[i] < demand_low:
                demand_low = None
                demand_high = None
                last_sweep_idx = -10_000
                last_sweep_low = None
                last_shift_idx = -10_000

            if i >= 12 and atr_now == atr_now and atr_now > 0:
                prior_low = float(lows[i - 12:i].min())
                prior_high = float(highs[i - 12:i].max())

                if demand_low is not None:
                    touched_demand = lows[i] <= demand_high * 1.002 and closes[i] > demand_low
                    sweep_now = lows[i] < prior_low and closes[i] > prior_low and touched_demand
                    if sweep_now:
                        last_sweep_idx = i
                        last_sweep_low = float(lows[i])

                    if 0 < i - last_sweep_idx <= 8 and closes[i] > prior_high:
                        last_shift_idx = i

                    liquidity_sweep_out[i] = 0 <= i - last_sweep_idx <= 8
                    market_shift_out[i] = 0 <= i - last_shift_idx <= 6

                    distance_from_zone = closes[i] - demand_high
                    demand_location_out[i] = (
                        closes[i] >= demand_high
                        and distance_from_zone >= 0
                        and distance_from_zone <= 3.0 * atr_now
                    )

                    structural_stop = demand_low - 0.20 * atr_now
                    risk = closes[i] - structural_stop
                    risk_pct = risk / closes[i] if closes[i] > 0 else float("inf")
                    structure_risk_pct_out[i] = risk_pct

                    prior_48_high = float(highs[max(0, i - 48):i].max()) if i > 0 else closes[i]
                    overhead = prior_48_high - closes[i]
                    room_2r_out[i] = (
                        risk > 0
                        and risk_pct <= 0.03
                        and (prior_48_high <= closes[i] or overhead >= 2.0 * risk)
                    )

                    demand_low_out[i] = demand_low
                    demand_high_out[i] = demand_high
                    if last_sweep_low is not None:
                        sweep_low_out[i] = last_sweep_low

                # Create a NEW zone only after evaluating the current bar against
                # any pre-existing zone, preventing same-bar self-confirmation.
                body = closes[i] - opens[i]
                displaced = (
                    closes[i] > prior_high
                    and body >= 0.80 * atr_now
                    and relvols[i] == relvols[i]
                    and relvols[i] >= 1.10
                    and i >= 1
                    and closes[i - 1] < opens[i - 1]
                )
                if displaced:
                    demand_low = float(lows[i - 1])
                    demand_high = float(opens[i - 1])
                    last_sweep_idx = -10_000
                    last_sweep_low = None
                    last_shift_idx = -10_000

        dataframe["demand_low"] = demand_low_out
        dataframe["demand_high"] = demand_high_out
        dataframe["sweep_low"] = sweep_low_out
        dataframe["liquidity_sweep_confirmed"] = liquidity_sweep_out
        dataframe["market_structure_confirmed"] = market_shift_out
        dataframe["demand_location_confirmed"] = demand_location_out
        dataframe["room_for_2r"] = room_2r_out
        dataframe["structure_risk_pct"] = structure_risk_pct_out
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe["close"]

        # Williams Alligator, causal: no forward plotting offsets.
        dataframe["jaw"] = self._smma(close, 13)
        dataframe["teeth"] = self._smma(close, 8)
        dataframe["lips"] = self._smma(close, 5)
        dataframe["alligator_spread"] = (dataframe["lips"] - dataframe["jaw"]) / close

        # Momentum.
        dataframe["ema12"] = ta.EMA(dataframe, timeperiod=12)
        dataframe["ema26"] = ta.EMA(dataframe, timeperiod=26)
        dataframe["macd"] = dataframe["ema12"] - dataframe["ema26"]
        dataframe["macd_signal"] = dataframe["macd"].ewm(span=9, adjust=False, min_periods=9).mean()
        dataframe["macd_hist"] = dataframe["macd"] - dataframe["macd_signal"]

        # Direction / volatility / trend.
        dataframe["sar"] = ta.SAR(dataframe, acceleration=0.02, maximum=0.2)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)

        # 20 x 4h ~= 320 x 15m time-horizon proxy for validator parity.
        # Runtime Hummingbot also checks the actual 4h EMA20 from Binance.
        dataframe["htf_ema_proxy"] = ta.EMA(dataframe, timeperiod=320)

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
        dataframe["htf_proxy_confirmed"] = (
            (dataframe["close"] > dataframe["htf_ema_proxy"])
            & (dataframe["htf_ema_proxy"] > dataframe["htf_ema_proxy"].shift(1))
        )

        return self._add_structure_context(dataframe)

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = None

        setup = (
            dataframe["alligator_bull"]
            & dataframe["macd_bull"]
            & dataframe["sar_bull"]
            & dataframe["volume_confirmed"]
            & dataframe["trend_confirmed"]
            & dataframe["htf_proxy_confirmed"]
            & dataframe["liquidity_sweep_confirmed"]
            & dataframe["market_structure_confirmed"]
            & dataframe["demand_location_confirmed"]
            & dataframe["room_for_2r"]
            & (dataframe["structure_risk_pct"] > 0)
            & (dataframe["structure_risk_pct"] <= 0.03)
            & (dataframe["atr"] > 0)
            & (dataframe["volume"] > 0)
        )

        dataframe.loc[setup, ["enter_long", "enter_tag"]] = (1, "alligator_smc_v2")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = None

        alligator_failure = dataframe["lips"] < dataframe["teeth"]
        macd_failure = dataframe["macd"] < dataframe["macd_signal"]
        sar_flip = dataframe["sar"] > dataframe["close"]
        htf_failure = dataframe["close"] < dataframe["htf_ema_proxy"]

        dataframe.loc[
            (alligator_failure | macd_failure | sar_flip | htf_failure)
            & (dataframe["volume"] > 0),
            ["exit_long", "exit_tag"],
        ] = (1, "trend_or_structure_failure")
        return dataframe
