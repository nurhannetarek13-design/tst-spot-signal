import unittest

from v2_bot.strategy import ema, evaluate_candidate


def make_candles(count: int = 60, breakout: bool = False):
    candles = []
    for i in range(count):
        close = 100.0 + i * 0.5
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


class StrategyTests(unittest.TestCase):
    def test_ema_trends_up(self):
        values = [float(i) for i in range(1, 80)]
        self.assertGreater(ema(values, 20), 0)
        self.assertGreater(ema(values, 20), ema(values, 50))

    def test_full_quality_candidate_scores_100(self):
        candidate = evaluate_candidate(
            symbol="TESTUSDT",
            candles_15m=make_candles(breakout=True),
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
        self.assertTrue(candidate.breakout)
        self.assertGreaterEqual(candidate.relative_volume, 1.5)
        self.assertGreaterEqual(candidate.taker_buy_ratio, 0.56)


if __name__ == "__main__":
    unittest.main()
