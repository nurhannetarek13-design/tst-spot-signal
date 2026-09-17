import unittest

from v2_bot.historical_replay import INTERVAL_MS, ReplayTrade, _stats, resample_15m


class HistoricalReplayTests(unittest.TestCase):
    @staticmethod
    def candle(open_time, open_price, high, low, close, quote_volume=1000.0):
        return {
            "open_time": float(open_time),
            "open": float(open_price),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": 10.0,
            "close_time": float(open_time + INTERVAL_MS - 1),
            "quote_volume": float(quote_volume),
            "trades": 20.0,
            "taker_buy_base": 6.0,
            "taker_buy_quote": float(quote_volume * 0.6),
        }

    def test_resample_requires_complete_contiguous_bucket(self):
        rows = [
            self.candle(i * INTERVAL_MS, 100 + i, 101 + i, 99 + i, 100.5 + i)
            for i in range(4)
        ]
        hourly = resample_15m(rows, 4)
        self.assertEqual(len(hourly), 1)
        self.assertEqual(hourly[0]["open"], 100.0)
        self.assertEqual(hourly[0]["close"], 103.5)
        self.assertEqual(hourly[0]["high"], 104.0)
        self.assertEqual(hourly[0]["low"], 99.0)
        self.assertEqual(hourly[0]["quote_volume"], 4000.0)

        missing = rows[:2] + rows[3:]
        self.assertEqual(resample_15m(missing, 4), [])

    def test_stats_are_fee_agnostic_input_and_conservative_ambiguity_is_loss(self):
        trades = [
            ReplayTrade("BTCUSDT", "pullback", 1, 2, 3, 100, 101, "take_profit", 0.08),
            ReplayTrade("ETHUSDT", "breakout_retest", 4, 5, 6, 100, 99, "stop_loss", -0.08),
            ReplayTrade("SOLUSDT", "pullback", 7, 8, 9, 100, 99, "ambiguous_stop_first", -0.08),
            ReplayTrade("BNBUSDT", "pullback", 10, 11, None, 100, None, "open_at_end", None),
        ]
        stats = _stats(trades)
        self.assertEqual(stats["closed"], 3)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 2)
        self.assertEqual(stats["ambiguous_stop_first"], 1)
        self.assertAlmostEqual(stats["profit_factor"], 0.5)
        self.assertAlmostEqual(stats["expectancy_usdt"], -0.02666667)
        self.assertEqual(stats["open_at_end"], 1)
        self.assertAlmostEqual(stats["max_drawdown_usdt"], 0.16)


if __name__ == "__main__":
    unittest.main()
