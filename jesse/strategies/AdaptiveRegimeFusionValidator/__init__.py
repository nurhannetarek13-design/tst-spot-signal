import numpy as np
from jesse.strategies import Strategy
import jesse.indicators as ta


class AdaptiveRegimeFusionValidator(Strategy):
    """Independent validator for TST_ALLIGATOR_TMV_V1.

    Long-only, causal, and intentionally free of the forward plotting shifts used
    by the visual Williams Alligator indicator.
    """

    STRATEGY_ID = "TST_ALLIGATOR_TMV_V1"

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

    def _state(self):
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
        psar = self._psar(highs, lows)

        spread = (lips - jaw) / np.maximum(closes, 1e-12)
        prior_vol = volumes[-21:-1]
        relvol = volumes[-1] / prior_vol.mean() if len(prior_vol) == 20 and prior_vol.mean() > 0 else 0.0

        return {
            "close": closes[-1],
            "jaw": jaw,
            "teeth": teeth,
            "lips": lips,
            "spread": spread,
            "macd": macd,
            "macd_signal": macd_signal,
            "ema200": ema200[-1],
            "psar": psar[-1],
            "relvol": relvol,
            "volume": volumes[-1],
        }

    def should_long(self) -> bool:
        if len(self.candles) < 220:
            return False

        s = self._state()
        alligator_bull = (
            s["lips"][-1] > s["teeth"][-1] > s["jaw"][-1]
            and s["lips"][-1] > s["lips"][-2]
            and s["teeth"][-1] > s["teeth"][-2]
            and s["jaw"][-1] > s["jaw"][-2]
            and s["spread"][-1] > s["spread"][-2]
        )
        macd_bull = s["macd"][-1] > s["macd_signal"][-1] and (s["macd"][-1] - s["macd_signal"][-1]) > 0
        sar_bull = s["psar"] < s["close"]
        volume_confirmed = s["relvol"] >= 1.20
        trend_confirmed = s["close"] > s["ema200"]

        return bool(
            alligator_bull
            and macd_bull
            and sar_bull
            and volume_confirmed
            and trend_confirmed
            and s["volume"] > 0
        )

    def should_short(self) -> bool:
        return False

    def go_long(self):
        entry = float(self.price)
        atr = float(ta.atr(self.candles, 14))
        if not np.isfinite(atr) or atr <= 0:
            return

        stop = max(0.0, entry - 2.0 * atr)
        target = entry + 2.0 * max(entry - stop, 0.0)
        size_usd = min(5.5, max(0.0, float(self.balance)))
        qty = max(size_usd / entry, 1e-8)

        self.buy = qty, entry
        self.stop_loss = qty, stop
        self.take_profit = qty, target

    def should_cancel_entry(self) -> bool:
        return True

    def should_exit(self) -> bool:
        if len(self.candles) < 30:
            return False
        s = self._state()
        alligator_failure = s["lips"][-1] < s["teeth"][-1]
        macd_failure = s["macd"][-1] < s["macd_signal"][-1]
        sar_flip = s["psar"] > s["close"]
        return bool(alligator_failure or macd_failure or sar_flip)

    def go_short(self):
        pass
