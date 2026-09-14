from typing import List
import aiohttp
import pandas as pd

from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase,
)


class TSTAlligatorTMVSignalConfig(DirectionalTradingControllerConfigBase):
    controller_name: str = "tst_alligator_tmv_signal"
    candles_connector: str = Field(default=None)
    candles_trading_pair: str = Field(default=None)
    interval: str = Field(default="15m")
    fusion_url: str = Field(default="http://fusion-master:8787")
    fusion_token: str = Field(default="")
    strategy_id: str = Field(default="TST_ALLIGATOR_SMC_V2")
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


class TSTAlligatorTMVSignalController(DirectionalTradingControllerBase):
    """Paper-only Alligator + SMC-context Spot controller.

    Core trigger:
      causal Alligator + MACD + PSAR + relative volume + EMA200.

    Context gate:
      pre-existing demand zone -> liquidity sweep -> bullish market shift/BOS,
      demand-location discipline, actual Binance 4h EMA20 bias, and >= 2R room.

    Fusion then adds BTC regime, L2, taker flow, spread and liquidity checks.
    """

    ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

    def __init__(self, config: TSTAlligatorTMVSignalConfig, *args, **kwargs):
        self.config = config
        self.max_records = 420
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

    @staticmethod
    def _smma(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        prev_close = df["close"].astype(float).shift(1)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        tr = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    @staticmethod
    def _psar(df: pd.DataFrame, step: float = 0.02, maximum: float = 0.2) -> pd.Series:
        highs = df["high"].astype(float).tolist()
        lows = df["low"].astype(float).tolist()
        n = len(highs)
        if n == 0:
            return pd.Series(dtype="float64", index=df.index)
        if n == 1:
            return pd.Series([lows[0]], index=df.index, dtype="float64")

        out = [0.0] * n
        bull = True
        af = step
        ep = highs[0]
        out[0] = lows[0]

        for i in range(1, n):
            psar = out[i - 1] + af * (ep - out[i - 1])
            if bull:
                psar = min(psar, lows[i - 1])
                if i > 1:
                    psar = min(psar, lows[i - 2])
                if lows[i] < psar:
                    bull = False
                    psar = ep
                    ep = lows[i]
                    af = step
                elif highs[i] > ep:
                    ep = highs[i]
                    af = min(maximum, af + step)
            else:
                psar = max(psar, highs[i - 1])
                if i > 1:
                    psar = max(psar, highs[i - 2])
                if highs[i] > psar:
                    bull = True
                    psar = ep
                    ep = highs[i]
                    af = step
                elif lows[i] < ep:
                    ep = lows[i]
                    af = min(maximum, af + step)
            out[i] = psar

        return pd.Series(out, index=df.index, dtype="float64")

    @staticmethod
    def _structure_context(
        df: pd.DataFrame,
        i: int,
        atr: pd.Series,
        relvol: pd.Series,
    ) -> dict:
        """Causal market-structure state as of completed candle i."""
        opens = df["open"].astype(float).to_numpy()
        highs = df["high"].astype(float).to_numpy()
        lows = df["low"].astype(float).to_numpy()
        closes = df["close"].astype(float).to_numpy()
        atrs = atr.astype(float).to_numpy()
        relvols = relvol.astype(float).to_numpy()

        demand_low = None
        demand_high = None
        demand_seed_idx = -10_000
        last_sweep_idx = -10_000
        last_sweep_low = None
        last_shift_idx = -10_000

        for j in range(0, i + 1):
            atr_now = atrs[j]
            zone_expired = demand_low is not None and j - demand_seed_idx > 96
            zone_invalidated = demand_low is not None and closes[j] < demand_low
            if zone_expired or zone_invalidated:
                demand_low = None
                demand_high = None
                demand_seed_idx = -10_000
                last_sweep_idx = -10_000
                last_sweep_low = None
                last_shift_idx = -10_000

            if j < 12 or not pd.notna(atr_now) or atr_now <= 0:
                continue

            prior_low = float(lows[j - 12:j].min())
            prior_high = float(highs[j - 12:j].max())

            if demand_low is not None:
                touched_demand = lows[j] <= demand_high * 1.002 and closes[j] > demand_low
                sweep_now = lows[j] < prior_low and closes[j] > prior_low and touched_demand
                if sweep_now:
                    last_sweep_idx = j
                    last_sweep_low = float(lows[j])

                if 0 < j - last_sweep_idx <= 8 and closes[j] > prior_high:
                    last_shift_idx = j

            body = closes[j] - opens[j]
            displaced = (
                demand_low is None
                and closes[j] > prior_high
                and body >= 0.80 * atr_now
                and pd.notna(relvols[j])
                and relvols[j] >= 1.10
                and j >= 1
                and closes[j - 1] < opens[j - 1]
            )
            if displaced:
                demand_low = float(lows[j - 1])
                demand_high = float(opens[j - 1])
                demand_seed_idx = j
                last_sweep_idx = -10_000
                last_sweep_low = None
                last_shift_idx = -10_000

        atr_now = float(atrs[i]) if pd.notna(atrs[i]) else 0.0
        if demand_low is None or demand_high is None or atr_now <= 0:
            return {
                "liquiditySweepConfirmed": False,
                "marketStructureConfirmed": False,
                "demandLocationConfirmed": False,
                "roomFor2R": False,
                "demandZoneLow": 0.0,
                "demandZoneHigh": 0.0,
                "sweepLow": 0.0,
                "structureStop": 0.0,
                "structureRiskPct": 999.0,
            }

        entry = float(closes[i])
        liquidity_sweep_confirmed = 0 <= i - last_sweep_idx <= 8
        market_structure_confirmed = 0 <= i - last_shift_idx <= 6
        distance_from_zone = entry - demand_high
        demand_location_confirmed = (
            entry >= demand_high
            and distance_from_zone >= 0
            and distance_from_zone <= 3.0 * atr_now
        )

        invalidation_low = min(
            demand_low,
            last_sweep_low if last_sweep_low is not None else demand_low,
        )
        structure_stop = max(0.0, invalidation_low - 0.20 * atr_now)
        risk = entry - structure_stop
        risk_pct = risk / entry if entry > 0 and risk > 0 else 999.0

        prior_48_high = float(highs[max(0, i - 48):i].max()) if i > 0 else entry
        overhead = prior_48_high - entry
        room_for_2r = bool(
            risk > 0
            and risk_pct <= 0.03
            and (prior_48_high <= entry or overhead >= 2.0 * risk)
        )

        return {
            "liquiditySweepConfirmed": bool(liquidity_sweep_confirmed),
            "marketStructureConfirmed": bool(market_structure_confirmed),
            "demandLocationConfirmed": bool(demand_location_confirmed),
            "roomFor2R": room_for_2r,
            "demandZoneLow": float(demand_low),
            "demandZoneHigh": float(demand_high),
            "sweepLow": float(last_sweep_low or 0.0),
            "structureStop": float(structure_stop),
            "structureRiskPct": float(risk_pct),
        }

    async def _public_json(self, session: aiohttp.ClientSession, path: str, params: dict):
        url = f"{self.config.binance_public_url.rstrip('/')}{path}"
        async with session.get(url, params=params) as response:
            if response.status != 200:
                raise RuntimeError(f"BINANCE_PUBLIC_HTTP_{response.status}")
            return await response.json()

    async def _market_confirmations(self, symbol: str) -> dict:
        fail = {
            "l2Confirmed": False,
            "btcRegimeOk": False,
            "liquidityOk": False,
            "htfBiasOk": False,
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
                klines = await self._public_json(
                    session, "/api/v3/klines", {"symbol": symbol, "interval": "15m", "limit": 25}
                )
                htf_klines = await self._public_json(
                    session, "/api/v3/klines", {"symbol": symbol, "interval": "4h", "limit": 25}
                )
                btc_klines = klines if symbol == "BTCUSDT" else await self._public_json(
                    session, "/api/v3/klines", {"symbol": "BTCUSDT", "interval": "15m", "limit": 210}
                )

            bids = [(float(p), float(q)) for p, q in depth.get("bids", [])]
            asks = [(float(p), float(q)) for p, q in depth.get("asks", [])]
            if (
                not bids
                or not asks
                or len(klines) < 22
                or len(htf_klines) < 22
                or len(btc_klines) < 202
            ):
                return {**fail, "marketDataError": "PUBLIC_DATA_INCOMPLETE"}

            best_bid, best_ask = bids[0][0], asks[0][0]
            mid = (best_bid + best_ask) / 2.0
            spread_bps = ((best_ask - best_bid) / mid) * 10000.0 if mid > 0 else 999999.0
            bid_depth = sum(p * q for p, q in bids)
            ask_depth = sum(p * q for p, q in asks)
            total_depth = bid_depth + ask_depth
            bid_share = bid_depth / total_depth if total_depth > 0 else 0.0
            visible_depth = min(bid_depth, ask_depth)

            closed = klines[:-1]
            last = closed[-1]
            quote_volume = float(last[7])
            taker_buy_quote = float(last[10])
            taker_share = taker_buy_quote / quote_volume if quote_volume > 0 else 0.0
            prior_quote_volumes = [float(x[7]) for x in closed[-21:-1]]
            avg_prior_volume = (
                sum(prior_quote_volumes) / len(prior_quote_volumes)
                if prior_quote_volumes
                else 0.0
            )
            relative_volume = quote_volume / avg_prior_volume if avg_prior_volume > 0 else 0.0

            btc_closed = btc_klines[:-1]
            btc_closes = pd.Series([float(x[4]) for x in btc_closed], dtype="float64")
            btc_ema200 = btc_closes.ewm(span=200, adjust=False, min_periods=200).mean()
            btc_regime_ok = bool(
                len(btc_ema200)
                and pd.notna(btc_ema200.iloc[-1])
                and btc_closes.iloc[-1] > btc_ema200.iloc[-1]
            )

            htf_closed = htf_klines[:-1]
            htf_closes = pd.Series([float(x[4]) for x in htf_closed], dtype="float64")
            htf_ema20 = htf_closes.ewm(span=20, adjust=False, min_periods=20).mean()
            htf_bias_ok = bool(
                len(htf_ema20) >= 2
                and pd.notna(htf_ema20.iloc[-1])
                and pd.notna(htf_ema20.iloc[-2])
                and htf_closes.iloc[-1] > htf_ema20.iloc[-1]
                and htf_ema20.iloc[-1] > htf_ema20.iloc[-2]
            )

            liquidity_ok = bool(
                spread_bps <= 20.0
                and visible_depth >= self.config.min_visible_depth_usdt
            )
            l2_confirmed = bool(bid_share >= self.config.min_l2_bid_share)
            return {
                "l2Confirmed": l2_confirmed,
                "btcRegimeOk": btc_regime_ok,
                "liquidityOk": liquidity_ok,
                "htfBiasOk": htf_bias_ok,
                "takerBuyShare": float(taker_share),
                "relativeVolume": float(relative_volume),
                "spreadBps": float(spread_bps),
                "l2BidShare": float(bid_share),
                "visibleDepthUSDT": float(visible_depth),
            }
        except Exception as exc:
            return {
                **fail,
                "marketDataError": f"{type(exc).__name__}:{str(exc)[:80]}",
            }

    async def _fusion_decision(self, candidate: dict) -> dict:
        if not self.config.fusion_token:
            return {
                "decision": "NO_TRADE",
                "executorAllowed": False,
                "reasons": ["FUSION_TOKEN_MISSING"],
            }
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
                        return {
                            "decision": "NO_TRADE",
                            "executorAllowed": False,
                            "reasons": [f"FUSION_HTTP_{response.status}"],
                        }
                    return await response.json()
        except Exception as exc:
            return {
                "decision": "NO_TRADE",
                "executorAllowed": False,
                "reasons": [f"FUSION_ERROR:{type(exc).__name__}"],
            }

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
            self.processed_data["fusion"] = {
                "decision": "NO_TRADE",
                "reasons": ["SYMBOL_NOT_IN_V1_UNIVERSE"],
            }
            return
        if df is None or len(df) < 360:
            self.processed_data["signal"] = 0
            self.processed_data["fusion"] = {
                "decision": "NO_TRADE",
                "reasons": ["HISTORY"],
            }
            return

        df = df.copy()
        close = df["close"].astype(float)
        volume = df["volume"].astype(float)
        jaw = self._smma(close, 13)
        teeth = self._smma(close, 8)
        lips = self._smma(close, 5)
        spread = (lips - jaw) / close
        ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
        ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
        macd = ema12 - ema26
        macd_signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
        ema200 = close.ewm(span=200, adjust=False, min_periods=200).mean()
        htf_proxy = close.ewm(span=320, adjust=False, min_periods=320).mean()
        atr = self._atr(df, 14)
        psar = self._psar(df)
        relvol = volume / volume.rolling(20).mean().shift(1)

        # Last completed candle. The newest dataframe row can still be live.
        i = len(df) - 2
        prev = i - 1
        alligator_trigger = bool(
            pd.notna(jaw.iloc[i])
            and lips.iloc[i] > teeth.iloc[i] > jaw.iloc[i]
            and lips.iloc[i] > lips.iloc[prev]
            and teeth.iloc[i] > teeth.iloc[prev]
            and jaw.iloc[i] > jaw.iloc[prev]
            and spread.iloc[i] > spread.iloc[prev]
        )
        macd_confirmed = bool(
            pd.notna(macd_signal.iloc[i])
            and macd.iloc[i] > macd_signal.iloc[i]
            and (macd.iloc[i] - macd_signal.iloc[i]) > 0
        )
        sar_confirmed = bool(pd.notna(psar.iloc[i]) and psar.iloc[i] < close.iloc[i])
        volume_confirmed = bool(
            pd.notna(relvol.iloc[i]) and relvol.iloc[i] >= 1.20
        )
        trend_confirmed = bool(
            pd.notna(ema200.iloc[i]) and close.iloc[i] > ema200.iloc[i]
        )
        htf_proxy_confirmed = bool(
            pd.notna(htf_proxy.iloc[i])
            and pd.notna(htf_proxy.iloc[prev])
            and close.iloc[i] > htf_proxy.iloc[i]
            and htf_proxy.iloc[i] > htf_proxy.iloc[prev]
        )

        structure = self._structure_context(df, i, atr, relvol)
        core_trigger = (
            alligator_trigger
            and macd_confirmed
            and sar_confirmed
            and volume_confirmed
            and trend_confirmed
            and htf_proxy_confirmed
            and structure["liquiditySweepConfirmed"]
            and structure["marketStructureConfirmed"]
            and structure["demandLocationConfirmed"]
            and structure["roomFor2R"]
            and 0 < structure["structureRiskPct"] <= 0.03
        )

        self.processed_data["features"] = df
        if not core_trigger:
            reasons = []
            if not alligator_trigger:
                reasons.append("ALLIGATOR_NOT_OPEN")
            if not macd_confirmed:
                reasons.append("MACD_NOT_CONFIRMED")
            if not sar_confirmed:
                reasons.append("SAR_NOT_CONFIRMED")
            if not volume_confirmed:
                reasons.append("VOLUME_NOT_CONFIRMED")
            if not trend_confirmed:
                reasons.append("EMA200_TREND_BLOCK")
            if not htf_proxy_confirmed:
                reasons.append("HTF_PROXY_BLOCK")
            if not structure["liquiditySweepConfirmed"]:
                reasons.append("NO_DEMAND_LIQUIDITY_SWEEP")
            if not structure["marketStructureConfirmed"]:
                reasons.append("NO_BULLISH_MARKET_SHIFT")
            if not structure["demandLocationConfirmed"]:
                reasons.append("BAD_DEMAND_LOCATION")
            if not structure["roomFor2R"]:
                reasons.append("NO_2R_ROOM")
            if not (0 < structure["structureRiskPct"] <= 0.03):
                reasons.append("STRUCTURE_STOP_TOO_WIDE")
            self.processed_data["signal"] = 0
            self.processed_data["fusion"] = {
                "decision": "NO_TRADE",
                "reasons": reasons,
            }
            return

        confirmations = await self._market_confirmations(symbol)
        entry = float(close.iloc[i])
        stop = float(structure["structureStop"])
        risk = entry - stop
        if risk <= 0:
            self.processed_data["signal"] = 0
            self.processed_data["fusion"] = {
                "decision": "NO_TRADE",
                "reasons": ["STRUCTURE_STOP_INVALID"],
            }
            return

        # >=2R by construction. Position notional shrinks if the structural stop
        # would otherwise exceed the configured USD risk cap.
        target = entry + 2.0 * risk
        risk_pct = risk / entry
        risk_limited_notional = self.config.max_risk_usd / risk_pct
        notional = min(self.config.order_amount_usd, 7.0, risk_limited_notional)
        risk_usd = notional * risk_pct

        score = 90
        if confirmations["l2Confirmed"]:
            score += 2
        if confirmations["takerBuyShare"] >= 0.56:
            score += 2
        if confirmations["relativeVolume"] >= 1.20:
            score += 1
        if confirmations["btcRegimeOk"]:
            score += 1
        if confirmations["liquidityOk"]:
            score += 1
        if confirmations["htfBiasOk"]:
            score += 3
        score = min(score, 100)

        candidate = {
            "strategyId": self.config.strategy_id,
            "symbol": symbol,
            "side": "LONG",
            "score": score,
            "regime": "BTC_BULL" if confirmations["btcRegimeOk"] else "BTC_BLOCKED",
            "setup": "alligator_tmv_demand_sweep_market_shift",
            "entry": entry,
            "stop": stop,
            "target": target,
            "notionalUSDT": notional,
            "riskUSDT": risk_usd,
            "riskReward": 2.0,
            "alligatorTrigger": True,
            "macdConfirmed": True,
            "sarConfirmed": True,
            "trendConfirmed": True,
            "htfProxyConfirmed": True,
            **structure,
            **confirmations,
        }

        fusion = await self._fusion_decision(candidate)
        executor_allowed = (
            bool(fusion.get("executorAllowed"))
            and self.config.allow_executor_actions
        )
        self.processed_data["signal"] = 1 if executor_allowed else 0
        self.processed_data["candidate"] = candidate
        self.processed_data["fusion"] = fusion
