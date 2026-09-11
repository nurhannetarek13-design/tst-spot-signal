import unittest

from v2_bot.config import Settings
from v2_bot.engine import V2Engine


class EngineUniverseTests(unittest.TestCase):
    def make_engine(self):
        engine = object.__new__(V2Engine)
        engine.settings = Settings(universe_limit=10, min_quote_volume_24h=20_000_000.0)
        return engine

    def test_excludes_stablecoin_and_fiat_base_pairs(self):
        engine = self.make_engine()
        tickers = [
            {"symbol": "BTCUSDT", "quoteVolume": "1000000000"},
            {"symbol": "USDCUSDT", "quoteVolume": "900000000"},
            {"symbol": "FDUSDUSDT", "quoteVolume": "800000000"},
            {"symbol": "EURUSDT", "quoteVolume": "700000000"},
            {"symbol": "SOLUSDT", "quoteVolume": "600000000"},
        ]
        universe = engine._build_universe(tickers)
        symbols = [symbol for symbol, _ in universe]

        self.assertIn("BTCUSDT", symbols)
        self.assertIn("SOLUSDT", symbols)
        self.assertNotIn("USDCUSDT", symbols)
        self.assertNotIn("FDUSDUSDT", symbols)
        self.assertNotIn("EURUSDT", symbols)

    def test_keeps_volume_filter_and_ranking(self):
        engine = self.make_engine()
        tickers = [
            {"symbol": "SOLUSDT", "quoteVolume": "600000000"},
            {"symbol": "ETHUSDT", "quoteVolume": "1000000000"},
            {"symbol": "TESTUSDT", "quoteVolume": "1000000"},
        ]
        universe = engine._build_universe(tickers)
        self.assertEqual(universe, [("ETHUSDT", 1_000_000_000.0), ("SOLUSDT", 600_000_000.0)])


if __name__ == "__main__":
    unittest.main()
