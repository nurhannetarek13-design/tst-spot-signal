import unittest

from research.binance_spot_point_in_time_universe import build


class PointInTimeUniverseTests(unittest.TestCase):
    def test_archive_presence_includes_delisted_symbols(self):
        symbols=["BTCUSDT","OLDUSDT","USDCUSDT","BTCUPUSDT"]
        files=[
            "BTCUSDT/15m/BTCUSDT-15m-2025-01.zip",
            "BTCUSDT/15m/BTCUSDT-15m-2025-02.zip",
            "OLDUSDT/15m/OLDUSDT-15m-2025-01.zip",
        ]
        info={"symbols":[
            {"symbol":"BTCUSDT","status":"TRADING","quoteAsset":"USDT","isSpotTradingAllowed":True},
            {"symbol":"OLDUSDT","status":"BREAK","quoteAsset":"USDT","isSpotTradingAllowed":False},
        ]}
        x=build(symbols,files,info)
        self.assertTrue(x["pointInTimeUniverse"])
        self.assertTrue(x["delistedCoverage"])
        self.assertIn("OLDUSDT",x["delistedSymbols"])
        self.assertIn("OLDUSDT",x["pointInTimeUniverseByMonth"]["2025-01"])
        self.assertNotIn("OLDUSDT",x["pointInTimeUniverseByMonth"]["2025-02"])
        self.assertNotIn("USDCUSDT",x["symbolLifetimes"])
        self.assertNotIn("BTCUPUSDT",x["symbolLifetimes"])


if __name__=="__main__":
    unittest.main()
