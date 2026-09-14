import numpy as np
from jesse.strategies import Strategy


class AdaptiveRegimeFusionValidator(Strategy):
    """Independent validator for TST_ALLIGATOR_SMC_V2.

    Long-only and causal. It mirrors the indicator + market-structure signal path:
    Alligator/MACD/PSAR/volume + pre-existing demand zone + liquidity sweep +
    bullish market shift + higher-time-horizon trend proxy + minimum 2R room.
    """

    STRATEGY_ID = "TST_ALLIGATOR_SMC_V2"

    @staticmethod
    def _ema(values, period: int):
        values = np.asarray(values, dtype=float)
        out = np.empty(len(values), dtype=float)
        alpha = 2.0 / (period + 1.0)
        out[0] = values[0]
        for i in range(1, len(values)):
            out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
        return out

    @staticmethod
    def _smma(values, period: int):
        values = np.asarray(values, dtype=float)
        out = np.empty(len(values), dtype=float)
        alpha = 1.0 / float(period)
        out[0] = values[0]
        for i in range(1, len(values)):
            out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
        return out

    @staticmethod
    def _atr_series(highs, lows, closes, period=14):
        highs = np.asarray(highs, dtype=float)
        lows = np.asarray(lows, dtype=float)
        closes = np.asarray(closes, dtype=float)
        tr = np.empty(len(closes), dtype=float)
        tr[0] = highs[0] - lows[0]
        for i in range(1, len(closes)):
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        return AdaptiveRegimeFusionValidator._smma(tr, period)

    @staticmethod
    def _psar(highs, lows, step=0.02, maximum=0.2):
        highs = np.asarray(highs, dtype=float)
        lows = np.asarray(lows, dtype=float)
        n = len(highs)
        out = np.zeros(n, dtype=float)
        if n == 0:
            return out
        if n == 1:
            out[0] = lows[0]
            return out

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
        return out

    @staticmethod
    def _relvol_series(volumes, period=20):
        volumes = np.asarray(volumes, dtype=float)
        out = np.zeros(len(volumes), dtype=float)
        for i in range(period, len(volumes)):
            prior = volumes[i - period:i]
            mean = prior.mean()
            out[i] = volumes[i] / mean if mean > 0 else 0.0
        return out

    @staticmethod
    def _structure_context(opens, highs, lows, closes, atrs, relvols):
        n = len(closes)
        demand_low = None
        demand_high = None
        demand_seed_idx = -10_000
        last_sweep_idx = -10_000
        last_sweep_low = None
        last_shift_idx = -10_000

        for i in range(n):
            atr_now = atrs[i]
            zone_expired = demand_low is not None and i - demand_seed_idx > 96
            zone_invalidated = demand_low is not None and closes[i] < demand_low
            if zone_expired or zone_invalidated:
                demand_low = None
                demand_high = None
                demand_seed_idx = -10_000
                last_sweep_idx = -10_000
                last_sweep_low = None
                last_shift_idx = -10_000

            if i < 12 or not np.isfinite(atr_now) or atr_now <= 0:
                continue

            prior_low = float(np.min(lows[i - 12:i]))
            prior_high = float(np.max(highs[i - 12:i]))

            if demand_low is not None:
                touched_demand = lows[i] <= demand_high * 1.002 and closes[i] > demand_low
                sweep_now = lows[i] < prior_low and closes[i] > prior_low and touched_demand
                if sweep_now:
                    last_sweep_idx = i
                    last_sweep_low = float(lows[i])

                if 0 < i - last_sweep_idx <= 8 and closes[i] > prior_high:
                    last_shift_idx = i

            body = closes[i] - opens[i]
            displaced = (
                demand_low is None
                and closes[i] > prior_high
                and body >= 0.80 * atr_now
                and relvols[i] >= 1.10
                and i >= 1
                and closes[i - 1] < opens[i - 1]
            )
            if displaced:
                demand_low = float(lows[i - 1])
                demand_high = float(opens[i - 1])
                demand_seed_idx = i
                last_sweep_idx = -10_000
                last_sweep_low = None
                last_shift_idx = -10_000

        i = n - 1
        atr_now = atrs[i]
        if (
            demand_low is None
            or demand_high is None
            or not np.isfinite(atr_now)
            or atr_now <= 0
        ):
            return {
                "liquidity_sweep": False,
                "market_shift": False,
                "demand_location": False,
                "room_2r": False,
                "demand_low": 0.0,
                "demand_high": 0.0,
                "sweep_low": 0.0,
                "structure_stop": 0.0,
                "structure_risk_pct": 999.0,
            }

        entry = float(closes[i])
        liquidity_sweep = 0 <= i - last_sweep_idx <= 8
        market_shift = 0 <= i - last_shift_idx <= 6
        distance = entry - demand_high
        demand_location = (
            entry >= demand_high
            and distance >= 0
            and distance <= 3.0 * atr_now
        )
        invalidation_low = min(
            demand_low,
            last_sweep_low if last_sweep_low is not None else demand_low,
        )
        structure_stop = max(0.0, invalidation_low - 0.20 * atr_now)
        risk = entry - structure_stop
        risk_pct = risk / entry if entry > 0 and risk > 0 else 999.0
        prior_48_high = float(np.max(highs[max(0, i - 48):i])) if i > 0 else entry
        overhead = prior_48_high - entry
        room_2r = bool(
            risk > 0
            and risk_pct <= 0.03
            and (prior_48_high <= entry or overhead >= 2.0 * risk)
        )

        return {
            "liquidity_sweep": bool(liquidity_sweep),
            "market_shift": bool(market_shift),
            "demand_location": bool(demand_location),
            "room_2r": room_2r,
            "demand_low": float(demand_low),
            "demand_high": float(demand_high),
            "sweep_low": float(last_sweep_low or 0.0),
            "structure_stop": float(structure_stop),
            "structure_risk_pct": float(risk_pct),
        }

    def _state(self):
        opens = self.candles[:, 1].astype(float)
        closes = self.candles[:, 2].astype(float)
        highs = self.candles[:, 3].astype(float)
        lows = self.candles[:, 4].astype(float)
        volumes = self.candles[:, 5].astype(float)

        jaw = self._smma(closes, 13)
        teeth = self._smma(closes, 8)
        lips = self._smma(closes, 5)
        ema12 = self._ema(closes, 12)
        ema26 = self._ema(closes, 26)
        macd = ema12 - ema26
        macd_signal = self._ema(macd, 9)
        ema200 = self._ema(closes, 200)
        htf_proxy = self._ema(closes, 320)
        psar = self._psar(highs, lows)
        atrs = self._atr_series(highs, lows, closes, 14)
        relvols = self._relvol_series(volumes, 20)
        spread = (lips - jaw) / np.maximum(closes, 1e-12)
        structure = self._structure_context(
            opens, highs, lows, closes, atrs, relvols
        )

        return {
            "close": closes[-1],
            "volume": volumes[-1],
            "jaw": jaw,
            "teeth": teeth,
            "lips": lips,
            "spread": spread,
            "macd": macd,
            "macd_signal": macd_signal,
            "ema200": ema200,
            "htf_proxy": htf_proxy,
            "psar": psar[-1],
            "atr": atrs[-1],
            "relvol": relvols[-1],
            **structure,
        }

    def should_long(self) -> bool:
        if len(self.candles) < 360:
            return False

        s = self._state()
        alligator_bull = (
            s["lips"][-1] > s["teeth"][-1] > s["jaw"][-1]
            and s["lips"][-1] > s["lips"][-2]
            and s["teeth"][-1] > s["teeth"][-2]
            and s["jaw"][-1] > s["jaw"][-2]
            and s["spread"][-1] > s["spread"][-2]
        )
        macd_bull = (
            s["macd"][-1] > s["macd_signal"][-1]
            and (s["macd"][-1] - s["macd_signal"][-1]) > 0
        )
        sar_bull = s["psar"] < s["close"]
        volume_confirmed = s["relvol"] >= 1.20
        trend_confirmed = s["close"] > s["ema200"][-1]
        htf_proxy_confirmed = (
            s["close"] > s["htf_proxy"][-1]
            and s["htf_proxy"][-1] > s["htf_proxy"][-2]
        )

        return bool(
            alligator_bull
            and macd_bull
            and sar_bull
            and volume_confirmed
            and trend_confirmed
            and htf_proxy_confirmed
            and s["liquidity_sweep"]
            and s["market_shift"]
            and s["demand_location"]
            and s["room_2r"]
            and 0 < s["structure_risk_pct"] <= 0.03
            and s["volume"] > 0
        )

    def should_short(self) -> bool:
        return False

    def go_long(self):
        s = self._state()
        entry = float(self.price)
        stop = float(s["structure_stop"])
        risk = entry - stop
        if risk <= 0 or entry <= 0:
            return

        risk_pct = risk / entry
        if risk_pct <= 0 or risk_pct > 0.03:
            return

        target = entry + 2.0 * risk
        risk_limited_notional = 0.20 / risk_pct
        size_usd = min(5.5, max(0.0, float(self.balance)), risk_limited_notional)
        if size_usd <= 0:
            return
        qty = max(size_usd / entry, 1e-8)

        self.buy = qty, entry
        self.stop_loss = qty, stop
        self.take_profit = qty, target

    def should_cancel_entry(self) -> bool:
        return True

    def should_exit(self) -> bool:
        if len(self.candles) < 360:
            return False
        s = self._state()
        alligator_failure = s["lips"][-1] < s["teeth"][-1]
        macd_failure = s["macd"][-1] < s["macd_signal"][-1]
        sar_flip = s["psar"] > s["close"]
        htf_failure = s["close"] < s["htf_proxy"][-1]
        structure_failure = (
            s["demand_low"] > 0 and s["close"] < s["demand_low"]
        )
        return bool(
            alligator_failure
            or macd_failure
            or sar_flip
            or htf_failure
            or structure_failure
        )

    def go_short(self):
        pass
