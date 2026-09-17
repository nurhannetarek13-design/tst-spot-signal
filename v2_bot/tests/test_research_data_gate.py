import unittest

from v2_bot.research_data_gate import INTERVAL_MS, ResearchDataError, validate_closed_history

BASE = 1800000000000 // INTERVAL_MS * INTERVAL_MS


def bar(i, **overrides):
    opened = BASE + i * INTERVAL_MS
    data = {"open_time": opened, "close_time": opened + INTERVAL_MS - 1,
            "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0,
            "volume": 1000.0, "quote_volume": 100000.0, "taker_buy_quote": 57000.0}
    data.update(overrides)
    return data


def audit(rows):
    return validate_closed_history("BTCUSDT", rows, start_ms=BASE,
                                   end_ms=BASE + 4 * INTERVAL_MS,
                                   as_of_ms=BASE + 4 * INTERVAL_MS)


class DataGateTests(unittest.TestCase):
    def test_complete_history_passes(self):
        self.assertEqual(len(audit([bar(i) for i in range(4)])), 4)

    def test_missing_middle_bar_fails(self):
        with self.assertRaisesRegex(ResearchDataError, "missing_duplicate"):
            audit([bar(0), bar(2), bar(3)])

    def test_duplicate_bar_fails(self):
        with self.assertRaises(ResearchDataError):
            audit([bar(0), bar(1), bar(1), bar(2), bar(3)])

    def test_missing_last_closed_bar_fails(self):
        with self.assertRaisesRegex(ResearchDataError, "missing_closed_bar"):
            audit([bar(0), bar(1), bar(2)])

    def test_forming_candle_dropped(self):
        self.assertEqual(len(audit([bar(i) for i in range(4)] + [bar(4)])), 4)

    def test_invalid_price_fails(self):
        with self.assertRaisesRegex(ResearchDataError, "invalid_ohlcv"):
            audit([bar(0), bar(1, low=105.0), bar(2), bar(3)])

    def test_invalid_taker_volume_fails(self):
        with self.assertRaisesRegex(ResearchDataError, "invalid_taker"):
            audit([bar(0), bar(1, taker_buy_quote=200000), bar(2), bar(3)])

    def test_unclosed_missing_bar_cannot_hide_gap(self):
        with self.assertRaises(ResearchDataError):
            audit([bar(0), bar(1), bar(2), bar(4)])

    def test_range_not_aligned_first_bar(self):
        rows = [bar(i) for i in range(1, 4)]
        self.assertEqual(len(validate_closed_history("BTCUSDT", rows,
            start_ms=BASE + 1, end_ms=BASE + 4 * INTERVAL_MS,
            as_of_ms=BASE + 4 * INTERVAL_MS)), 3)


if __name__ == "__main__":
    unittest.main()
