import unittest

from v2_bot.config import Settings
from v2_bot.engine import V2Engine


class UniverseStableFilterTests(unittest.TestCase):
    def make_engine(self):
        engine = object.__new__(V2Engine)
        engine.settings = Settings(universe_limit=20, min_quote_volume_24h=20_000_000.0)
        return engine

    def test_rlusd_is_excluded(self):
        engine = self.make_engine()
        tickers = [
            {"symbol": "RLUSDUSDT", "quoteVolume": "500000000"},
            {"symbol": "BTCUSDT", "quoteVolume": "400000000"},
        ]
        symbols = [symbol for symbol, _ in engine._build_universe(tickers)]
        self.assertNotIn("RLUSDUSDT", symbols)
        self.assertIn("BTCUSDT", symbols)

    def test_known_stables_remain_excluded(self):
        engine = self.make_engine()
        tickers = [
            {"symbol": "USDCUSDT", "quoteVolume": "500000000"},
            {"symbol": "FDUSDUSDT", "quoteVolume": "400000000"},
            {"symbol": "USDEUSDT", "quoteVolume": "300000000"},
            {"symbol": "RLUSDUSDT", "quoteVolume": "200000000"},
            {"symbol": "SOLUSDT", "quoteVolume": "100000000"},
        ]
        symbols = [symbol for symbol, _ in engine._build_universe(tickers)]
        self.assertEqual(symbols, ["SOLUSDT"])


if __name__ == "__main__":
    unittest.main()
