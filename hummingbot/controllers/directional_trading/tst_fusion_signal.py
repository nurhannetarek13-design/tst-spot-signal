from typing import List
import aiohttp
import pandas as pd
import pandas_ta as ta  # noqa: F401

from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase,
)


class TSTFusionSignalConfig(DirectionalTradingControllerConfigBase):
    controller_name: str = "tst_fusion_signal"
    candles_connector: str = Field(default=None)
    candles_trading_pair: str = Field(default=None)
    interval: str = Field(default="15m")
    fusion_url: str = Field(default="http://fusion-master:8787")
    fusion_token: str = Field(default="")
    strategy_id: str = Field(default="TST_MACD_EMA200_L2_V1")
    order_amount_usd: float = Field(default=5.5)
    max_risk_usd: float = Field(default=0.20)
    allow_executor_actions: bool = Field(default=False)
    binance_public_url: str = Field(default="https://api.binance.com")
    min_l2_bid_share: float = Field(default=0.52)
    min_visible_depth_usdt: float = Field(default=25000.0)

    @field_validator("candles_connector", mode="before")
    @classmethod
    def set_candles_connector(cls, v, validation_info: ValidationInfo):
        return validation_info.data.get("connector_name") if v is None or v == "" else v

    @field_validator("candles_trading_pair", mode="before")
    @classmethod
    def set_candles_trading_pair(cls, v, validation_info: ValidationInfo):
        return validation_info.data.get("trading_pair") if v is None or v == "" else v


class TSTFusionSignalController(DirectionalTradingControllerBase):
    """Paper-only MACD+EMA200 trigger with real public Binance L2 confirmation.

    No private Binance endpoints or API keys are used here. Market confirmation is
    fetched from public depth + public klines. Any missing/invalid datum fails closed.
    """

    ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

    def __init__(self, config: TSTFusionSignalConfig, *args, **kwargs):
        self.config = config
        self.max_records = 260
        super().__init__(config, *args, **kwargs)

    def get_candles_config(self) -> List[CandlesConfig]:
        return [
            CandlesConfig(
                connector=self.config.candles_connector,
                trading_pair=self.config.candles_trading_pair,
                interval=self.config.interval,
                max_records=self.max_records,
            )
        ]

    @staticmethod
    def _binance_symbol(pair: str) -> str:
        return str(pair or "").upper().replace("-", "").replace("/", "")

    async def _public_json(self, session: aiohttp.ClientSession, path: str, params: dict):
        url = f"{self.config.binance_public_url.rstrip('/')}{path}"
        async with session.get(url, params=params) as response:
            if response.status != 200:
                raise RuntimeError(f"BINANCE_PUBLIC_HTTP_{response.status}")
            return await response.json()

    async def _market_confirmations(self, symbol: str) -> dict:
        """Return real order-book/flow confirmations; fail closed on any error."""
        fail = {
            "l2Confirmed": False,
            "btcRegimeOk": False,
            "liquidityOk": False,
            "takerBuyShare": 0.0,
            "relativeVolume": 0.0,
            "spreadBps": 999999.0,
            "l2BidShare": 0.0,
            "visibleDepthUSDT": 0.0,
        }
        if symbol not in self.ALLOWED_SYMBOLS:
            return {**fail, "marketDataError": "SYMBOL_NOT_IN_V1_UNIVERSE"}

        timeout = aiohttp.ClientTimeout(total=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                depth = await self._public_json(session, "/api/v3/depth", {"symbol": symbol, "limit": 20})
                klines = await self._public_json(session, "/api/v3/klines", {"symbol": symbol, "interval": "15m", "limit": 25})
                btc_klines = klines if symbol == "BTCUSDT" else await self._public_json(
                    session, "/api/v3/klines", {"symbol": "BTCUSDT", "interval": "15m", "limit": 210}
                )

            bids = [(float(p), float(q)) for p, q in depth.get("bids", [])]
            asks = [(float(p), float(q)) for p, q in depth.get("asks", [])]
            if not bids or not asks or len(klines) < 22 or len(btc_klines) < 202:
                return {**fail, "marketDataError": "PUBLIC_DATA_INCOMPLETE"}

            best_bid, best_ask = bids[0][0], asks[0][0]
            mid = (best_bid + best_ask) / 2.0
            spread_bps = ((best_ask - best_bid) / mid) * 10000.0 if mid > 0 else 999999.0
            bid_depth = sum(p * q for p, q in bids)
            ask_depth = sum(p * q for p, q in asks)
            total_depth = bid_depth + ask_depth
            bid_share = bid_depth / total_depth if total_depth > 0 else 0.0
            visible_depth = min(bid_depth, ask_depth)

            # Binance REST klines include the still-open candle last. Use only closed bars.
            closed = klines[:-1]
            last = closed[-1]
            quote_volume = float(last[7])
            taker_buy_quote = float(last[10])
            taker_share = taker_buy_quote / quote_volume if quote_volume > 0 else 0.0
            prior_quote_volumes = [float(x[7]) for x in closed[-21:-1]]
            avg_prior_volume = sum(prior_quote_volumes) / len(prior_quote_volumes) if prior_quote_volumes else 0.0
            relative_volume = quote_volume / avg_prior_volume if avg_prior_volume > 0 else 0.0

            btc_closed = btc_klines[:-1]
            btc_closes = pd.Series([float(x[4]) for x in btc_closed], dtype="float64")
            btc_ema200 = btc_closes.ewm(span=200, adjust=False, min_periods=200).mean()
            btc_regime_ok = bool(len(btc_ema200) and pd.notna(btc_ema200.iloc[-1]) and btc_closes.iloc[-1] > btc_ema200.iloc[-1])

            liquidity_ok = bool(spread_bps <= 20.0 and visible_depth >= self.config.min_visible_depth_usdt)
            l2_confirmed = bool(bid_share >= self.config.min_l2_bid_share)
            return {
                "l2Confirmed": l2_confirmed,
                "btcRegimeOk": btc_regime_ok,
                "liquidityOk": liquidity_ok,
                "takerBuyShare": float(taker_share),
                "relativeVolume": float(relative_volume),
                "spreadBps": float(spread_bps),
                "l2BidShare": float(bid_share),
                "visibleDepthUSDT": float(visible_depth),
            }
        except Exception as exc:
            return {**fail, "marketDataError": f"{type(exc).__name__}:{str(exc)[:80]}"}

    async def _fusion_decision(self, candidate: dict) -> dict:
        if not self.config.fusion_token:
            return {"decision": "NO_TRADE", "executorAllowed": False, "reasons": ["FUSION_TOKEN_MISSING"]}
        headers = {"x-fusion-token": self.config.fusion_token}
        timeout = aiohttp.ClientTimeout(total=3)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.config.fusion_url.rstrip('/')}/candidate/hummingbot",
                    json=candidate,
                    headers=headers,
                ) as response:
                    if response.status != 200:
                        return {"decision": "NO_TRADE", "executorAllowed": False, "reasons": [f"FUSION_HTTP_{response.status}"]}
                    return await response.json()
        except Exception as exc:
            return {"decision": "NO_TRADE", "executorAllowed": False, "reasons": [f"FUSION_ERROR:{type(exc).__name__}"]}

    async def update_processed_data(self):
        df = self.market_data_provider.get_candles_df(
            connector_name=self.config.candles_connector,
            trading_pair=self.config.candles_trading_pair,
            interval=self.config.interval,
            max_records=self.max_records,
        )
        symbol = self._binance_symbol(self.config.trading_pair)

        if symbol not in self.ALLOWED_SYMBOLS:
            self.processed_data["signal"] = 0
            self.processed_data["fusion"] = {"decision": "NO_TRADE", "reasons": ["SYMBOL_NOT_IN_V1_UNIVERSE"]}
            return
        if df is None or len(df) < 210:
            self.processed_data["signal"] = 0
            self.processed_data["fusion"] = {"decision": "NO_TRADE", "reasons": ["HISTORY"]}
            return

        close = df["close"].astype(float)
        ema200 = close.ewm(span=200, adjust=False, min_periods=200).mean()
        ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
        ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
        macd = ema12 - ema26
        macd_signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
        df.ta.atr(length=14, append=True)
        atr = df["ATRr_14"] if "ATRr_14" in df.columns else df.filter(like="ATR").iloc[:, 0]

        # Use the last completed 15m candle; the last dataframe row can still be live.
        i = len(df) - 2
        prev = i - 1
        macd_trigger = bool(
            pd.notna(ema200.iloc[i])
            and pd.notna(macd_signal.iloc[i])
            and macd.iloc[i] > macd_signal.iloc[i]
            and macd.iloc[prev] <= macd_signal.iloc[prev]
            and macd.iloc[i] < 0
            and close.iloc[i] > ema200.iloc[i]
        )

        self.processed_data["features"] = df
        if not macd_trigger:
            self.processed_data["signal"] = 0
            self.processed_data["fusion"] = {"decision": "NO_TRADE", "reasons": ["NO_MACD_EMA200_TRIGGER"]}
            return

        confirmations = await self._market_confirmations(symbol)
        entry = float(close.iloc[i])
        atr_now = float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else 0.0
        if atr_now <= 0:
            self.processed_data["signal"] = 0
            self.processed_data["fusion"] = {"decision": "NO_TRADE", "reasons": ["ATR_INVALID"]}
            return

        stop = max(0.0, entry - 2.0 * atr_now)
        target = entry + 2.0 * (entry - stop)
        risk_pct = max((entry - stop) / entry, 0.0001)
        risk_usd = min(self.config.max_risk_usd, self.config.order_amount_usd * risk_pct)

        # Score is not used to disguise missing gates. Missing/weak market data is
        # rejected independently by Fusion; 90 only means the core trigger exists.
        score = 90
        if confirmations["l2Confirmed"]:
            score += 3
        if confirmations["takerBuyShare"] >= 0.56:
            score += 3
        if confirmations["relativeVolume"] >= 1.5:
            score += 2
        if confirmations["btcRegimeOk"] and confirmations["liquidityOk"]:
            score += 2
        score = min(score, 100)

        candidate = {
            "strategyId": self.config.strategy_id,
            "symbol": symbol,
            "side": "LONG",
            "score": score,
            "regime": "BTC_BULL" if confirmations["btcRegimeOk"] else "BTC_BLOCKED",
            "setup": "macd_ema200_pullback",
            "entry": entry,
            "stop": stop,
            "target": target,
            "notionalUSDT": min(self.config.order_amount_usd, 7.0),
            "riskUSDT": risk_usd,
            "macdTrigger": True,
            **confirmations,
        }

        fusion = await self._fusion_decision(candidate)

        # V1 is intentionally PAPER_ONLY. Even PAPER_APPROVED cannot create a real
        # executor until a future explicit release changes both policy and controller.
        executor_allowed = bool(fusion.get("executorAllowed")) and self.config.allow_executor_actions
        self.processed_data["signal"] = 1 if executor_allowed else 0
        self.processed_data["candidate"] = candidate
        self.processed_data["fusion"] = fusion
