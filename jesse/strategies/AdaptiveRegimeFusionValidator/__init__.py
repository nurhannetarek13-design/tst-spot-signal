from jesse.strategies import Strategy
import jesse.indicators as ta
from jesse import utils


class AdaptiveRegimeFusionValidator(Strategy):
    """
    Independent research validator for TST_ADAPTIVE_FUSION_V1.
    Long-only by design. This file is for backtesting/research, not live execution.
    """

    STRATEGY_ID = "TST_ADAPTIVE_FUSION_V1"

    def _signals(self):
        ema20 = ta.ema(self.candles, 20)
        ema50 = ta.ema(self.candles, 50)
        ema200 = ta.ema(self.candles, 200)
        rsi = ta.rsi(self.candles, 14)
        adx = ta.adx(self.candles, 14)
        atr = ta.atr(self.candles, 14)
        return ema20, ema50, ema200, rsi, adx, atr

    def should_long(self) -> bool:
        if len(self.candles) < 210:
            return False

        ema20, ema50, ema200, rsi, adx, atr = self._signals()
        closes = self.candles[:, 2]
        highs = self.candles[:, 3]
        lows = self.candles[:, 4]
        volumes = self.candles[:, 5]

        close = self.price
        open_price = self.candles[-1, 1]
        low = lows[-1]
        hh20 = highs[-21:-1].max()
        ll20 = lows[-21:-1].min()

        vol_window = volumes[-25:-1]
        vol_med = sorted(vol_window)[len(vol_window) // 2] if len(vol_window) else 0
        relvol = volumes[-1] / vol_med if vol_med > 0 else 0

        atr_pct_now = atr / close if close > 0 else 0
        atr_samples = []
        for offset in range(2, 50):
            price = closes[-offset]
            if price > 0:
                atr_samples.append(abs(closes[-offset] - closes[-offset - 1]) / price)
        atr_med = sorted(atr_samples)[len(atr_samples) // 2] if atr_samples else atr_pct_now

        trend_regime = close > ema200 and ema20 > ema50 and adx >= 22
        range_regime = adx < 19
        vol_regime = atr_pct_now > atr_med * 1.35 and relvol >= 1.5

        trend_breakout = trend_regime and close > hh20 and relvol >= 1.25 and 53 <= rsi <= 70
        trend_pullback = (
            trend_regime
            and low <= ema20 * 1.004
            and close > ema20
            and close > open_price
            and 48 <= rsi <= 65
        )
        mean_reversion = range_regime and close <= ll20 * 1.003 and rsi < 34 and relvol >= 0.8
        volatility_momentum = vol_regime and close > hh20 and close > ema50 and 55 <= rsi <= 73

        return trend_breakout or trend_pullback or mean_reversion or volatility_momentum

    def should_short(self) -> bool:
        return False

    def go_long(self):
        _, _, _, _, _, atr = self._signals()
        entry = self.price
        stop = max(0, entry - 1.2 * atr)
        target = entry + 3.0 * max(entry - stop, 0)

        size_usd = min(5.5, max(0, self.balance))
        qty = max(size_usd / entry, 1e-8)

        self.buy = qty, entry
        self.stop_loss = qty, stop
        self.take_profit = qty, target

    def go_short(self):
        pass

    def should_cancel_entry(self) -> bool:
        return True
