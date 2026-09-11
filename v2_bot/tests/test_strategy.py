import unittest

from v2_bot.strategy import ema, evaluate_candidate


def make_candles(count: int = 60, breakout: bool = False, rising: bool = True):
    candles = []
    for i in range(count):
        close = 100.0 + i * 0.5 if rising else 140.0 - i * 0.5
        quote_volume = 1000.0
        taker = 580.0
        candles.append(
            {
                "open_time": float(i),
                "open": close - 0.2,
                "high": close + 0.2,
                "low": close - 0.4,
                "close": close,
                "volume": 10.0,
                "quote_volume": quote_volume,
                "trades": 100.0,
                "taker_buy_base": 5.8,
                "taker_buy_quote": taker,
            }
        )
    if breakout:
        prev_high = max(c["high"] for c in candles[-21:-1])
        candles[-1]["close"] = prev_high + 1.0
        candles[-1]["high"] = prev_high + 1.2
        candles[-1]["quote_volume"] = 2000.0
        candles[-1]["taker_buy_quote"] = 1200.0
    return candles


def make_confirmed_breakout_retest_candles():
    candles = make_candles()
    # The penultimate candle already closes above the prior 20-candle high.
    # The newest candle retests that level, holds it, and closes bullish without
    # becoming another raw breakout itself.
    level = max(c["high"] for c in candles[-22:-2])
    candles[-2]["open"] = level - 0.1
    candles[-2]["low"] = level - 0.15
    candles[-2]["close"] = level + 0.3
    candles[-2]["high"] = level + 0.5

    candles[-1]["open"] = level + 0.05
    candles[-1]["low"] = level - 0.05
    candles[-1]["close"] = level + 0.3
    candles[-1]["high"] = level + 0.4
    candles[-1]["quote_volume"] = 2000.0
    candles[-1]["taker_buy_quote"] = 1200.0
    return candles


def make_confirmed_pullback_candles():
    candles = make_candles()
    # Preserve a prior swing high so the confirmation is not also a breakout.
    candles[-10]["high"] = 132.0

    # Previous candle clearly trades through the current EMA20 zone but closes
    # back above it, avoiding a borderline fixture that depends on rounding.
    candles[-2]["open"] = 126.0
    candles[-2]["high"] = 126.0
    candles[-2]["low"] = 123.0
    candles[-2]["close"] = 125.0

    # Latest closed candle confirms the reclaim above the previous candle high.
    candles[-1]["open"] = 125.8
    candles[-1]["high"] = 126.8
    candles[-1]["low"] = 125.5
    candles[-1]["close"] = 126.5
    candles[-1]["quote_volume"] = 2000.0
    candles[-1]["taker_buy_quote"] = 1200.0
    return candles


class StrategyTests(unittest.TestCase):
    def test_ema_trends_up(self):
        values = [float(i) for i in range(1, 80)]
        self.assertGreater(ema(values, 20), 0)
        self.assertGreater(ema(values, 20), ema(values, 50))

    def test_raw_breakout_is_diagnostic_only_not_entry(self):
        candles_15m = make_candles(breakout=True)
        candidate = evaluate_candidate(
            symbol="TESTUSDT",
            candles_15m=candles_15m,
            candles_1h=make_candles(),
            candles_4h=make_candles(),
            btc_1h=make_candles(),
            spread_bps=5.0,
            quote_volume_24h=100_000_000.0,
            min_quote_volume_24h=20_000_000.0,
            max_spread_bps=15.0,
            min_score=90,
        )
        self.assertTrue(candidate.breakout)
        self.assertFalse(candidate.breakout_retest)
        self.assertEqual(candidate.entry_setup, "none")
        self.assertEqual(candidate.score, 85)
        self.assertFalse(candidate.eligible)

    def test_confirmed_breakout_retest_scores_100(self):
        candles_15m = make_confirmed_breakout_retest_candles()
        candidate = evaluate_candidate(
            symbol="TESTUSDT",
            candles_15m=candles_15m,
            candles_1h=make_candles(),
            candles_4h=make_candles(),
            btc_1h=make_candles(),
            spread_bps=5.0,
            quote_volume_24h=100_000_000.0,
            min_quote_volume_24h=20_000_000.0,
            max_spread_bps=15.0,
            min_score=90,
        )
        self.assertEqual(candidate.score, 100)
        self.assertTrue(candidate.eligible)
        self.assertTrue(candidate.breakout_retest)
        self.assertEqual(candidate.entry_setup, "breakout_retest")
        self.assertGreater(candidate.breakout_level, 0.0)
        self.assertLessEqual(candidate.price, candidate.breakout_level * 1.01)

    def test_confirmed_pullback_can_score_100_without_breakout(self):
        candles_15m = make_confirmed_pullback_candles()
        candidate = evaluate_candidate(
            symbol="TESTUSDT",
            candles_15m=candles_15m,
            candles_1h=make_candles(),
            candles_4h=make_candles(),
            btc_1h=make_candles(),
            spread_bps=5.0,
            quote_volume_24h=100_000_000.0,
            min_quote_volume_24h=20_000_000.0,
            max_spread_bps=15.0,
            min_score=90,
        )
        self.assertEqual(candidate.score, 100)
        self.assertTrue(candidate.eligible)
        self.assertFalse(candidate.breakout)
        self.assertFalse(candidate.breakout_retest)
        self.assertTrue(candidate.pullback)
        self.assertEqual(candidate.entry_setup, "pullback")
        self.assertLess(candidate.price, candidate.previous_20_high)

    def test_score_90_is_not_eligible_when_one_mandatory_gate_fails(self):
        candidate = evaluate_candidate(
            symbol="TESTUSDT",
            candles_15m=make_confirmed_breakout_retest_candles(),
            candles_1h=make_candles(),
            candles_4h=make_candles(rising=False),
            btc_1h=make_candles(),
            spread_bps=5.0,
            quote_volume_24h=100_000_000.0,
            min_quote_volume_24h=20_000_000.0,
            max_spread_bps=15.0,
            min_score=90,
        )
        self.assertEqual(candidate.score, 90)
        self.assertFalse(candidate.trend_4h)
        self.assertFalse(candidate.eligible)

    def test_requires_enough_btc_closed_candles(self):
        with self.assertRaisesRegex(ValueError, "Not enough closed candles"):
            evaluate_candidate(
                symbol="TESTUSDT",
                candles_15m=make_confirmed_breakout_retest_candles(),
                candles_1h=make_candles(),
                candles_4h=make_candles(),
                btc_1h=make_candles(count=20),
                spread_bps=5.0,
                quote_volume_24h=100_000_000.0,
                min_quote_volume_24h=20_000_000.0,
                max_spread_bps=15.0,
                min_score=90,
            )


if __name__ == "__main__":
    unittest.main()
