from typing import List
import aiohttp
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
    strategy_id: str = Field(default="TST_ADAPTIVE_FUSION_V1")
    order_amount_usd: float = Field(default=5.5)
    max_risk_usd: float = Field(default=0.20)
    allow_executor_actions: bool = Field(default=False)

    @field_validator("candles_connector", mode="before")
    @classmethod
    def set_candles_connector(cls, v, validation_info: ValidationInfo):
        return validation_info.data.get("connector_name") if v is None or v == "" else v

    @field_validator("candles_trading_pair", mode="before")
    @classmethod
    def set_candles_trading_pair(cls, v, validation_info: ValidationInfo):
        return validation_info.data.get("trading_pair") if v is None or v == "" else v


class TSTFusionSignalController(DirectionalTradingControllerBase):
    def __init__(self, config: TSTFusionSignalConfig, *args, **kwargs):
        self.config = config
        self.max_records = 240
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

        if df is None or len(df) < 210:
            self.processed_data["signal"] = 0
            self.processed_data["fusion"] = {"decision": "NO_TRADE", "reasons": ["HISTORY"]}
            return

        df.ta.ema(length=20, append=True)
        df.ta.ema(length=50, append=True)
        df.ta.ema(length=200, append=True)
        df.ta.rsi(length=14, append=True)
        df.ta.adx(length=14, append=True)
        df.ta.atr(length=14, append=True)

        e20 = df["EMA_20"]
        e50 = df["EMA_50"]
        e200 = df["EMA_200"]
        rsi = df["RSI_14"]
        adx = df["ADX_14"]
        atr = df["ATRr_14"] if "ATRr_14" in df.columns else df.filter(like="ATR").iloc[:, 0]

        relvol = df["volume"] / df["volume"].rolling(24).median()
        atr_pct = atr / df["close"]
        atr_med = atr_pct.rolling(48).median()
        hh20 = df["high"].rolling(20).max().shift(1)
        ll20 = df["low"].rolling(20).min().shift(1)

        last = df.iloc[-1]
        i = len(df) - 1

        trend_regime = bool(last["close"] > e200.iloc[i] and e20.iloc[i] > e50.iloc[i] and adx.iloc[i] >= 22)
        range_regime = bool(adx.iloc[i] < 19)
        vol_regime = bool(atr_pct.iloc[i] > atr_med.iloc[i] * 1.35 and relvol.iloc[i] >= 1.5)

        breakout = bool(last["close"] > hh20.iloc[i] and relvol.iloc[i] >= 1.25 and 53 <= rsi.iloc[i] <= 70)
        pullback = bool(last["low"] <= e20.iloc[i] * 1.004 and last["close"] > e20.iloc[i] and last["close"] > last["open"] and 48 <= rsi.iloc[i] <= 65)
        range_reversal = bool(range_regime and last["close"] <= ll20.iloc[i] * 1.003 and rsi.iloc[i] < 34 and relvol.iloc[i] >= 0.8)
        vol_momentum = bool(vol_regime and last["close"] > hh20.iloc[i] and last["close"] > e50.iloc[i] and 55 <= rsi.iloc[i] <= 73)

        setup = None
        regime = "NO_TRADE"
        if trend_regime and breakout:
            setup, regime = "trend_breakout", "TREND"
        elif trend_regime and pullback:
            setup, regime = "trend_pullback", "TREND"
        elif range_reversal:
            setup, regime = "mean_reversion", "RANGE"
        elif vol_momentum:
            setup, regime = "volatility_momentum", "VOLATILITY"

        if setup is None:
            self.processed_data["signal"] = 0
            self.processed_data["features"] = df
            self.processed_data["fusion"] = {"decision": "NO_TRADE", "reasons": ["NO_SETUP"], "regime": regime}
            return

        score = 90
        if relvol.iloc[i] >= 1.5:
            score += 3
        if adx.iloc[i] >= 28:
            score += 3
        if last["close"] > e20.iloc[i]:
            score += 2
        score = min(score, 100)

        entry = float(last["close"])
        atr_now = float(atr.iloc[i])
        stop = max(0.0, entry - 1.2 * atr_now)
        target = entry + 3.0 * max(entry - stop, 0.0)
        risk_pct = max((entry - stop) / entry, 0.0001)
        risk_usd = min(self.config.max_risk_usd, self.config.order_amount_usd * risk_pct)

        candidate = {
            "strategyId": self.config.strategy_id,
            "symbol": self.config.trading_pair,
            "side": "LONG",
            "score": score,
            "regime": regime,
            "setup": setup,
            "entry": entry,
            "stop": stop,
            "target": target,
            "notionalUSDT": min(self.config.order_amount_usd, 7.0),
            "riskUSDT": risk_usd,
        }

        fusion = await self._fusion_decision(candidate)

        # Fail closed: the repository release is PAPER_ONLY. The master currently
        # never returns executorAllowed=true, so no real executor action can be emitted.
        executor_allowed = bool(fusion.get("executorAllowed")) and self.config.allow_executor_actions
        self.processed_data["signal"] = 1 if executor_allowed else 0
        self.processed_data["features"] = df
        self.processed_data["candidate"] = candidate
        self.processed_data["fusion"] = fusion
