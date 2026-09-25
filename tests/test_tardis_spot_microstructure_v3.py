import unittest

from research.tardis_spot_microstructure_v3 import replay, simulate_buy


class TardisSpotReplayV3Tests(unittest.TestCase):
    def test_quote_sized_fill_simulation(self):
        asks={100.0:0.05,101.0:1.0}
        x=simulate_buy(asks,10.0)
        self.assertGreaterEqual(x["fill_ratio"],0.999)
        self.assertGreater(x["slippage_bps"],0)

    def test_spot_snapshot_bridge_and_continuity(self):
        rows=[
            {"line":0,"prefix":"","data":{"lastUpdateId":100,"bids":[["99","10"]],"asks":[["101","10"]]}},
            {"line":1,"prefix":"","data":{"E":1000,"U":101,"u":102,"b":[["100","5"]],"a":[["101","9"]]}},
            {"line":2,"prefix":"","data":{"E":2000,"U":103,"u":103,"b":[["100","6"]],"a":[["102","2"]]}},
        ]
        x=replay(rows,"BTCUSDT",0,10_000,10)
        self.assertTrue(x["canonicalReplayReady"])
        self.assertEqual(x["gaps"],0)
        self.assertEqual(x["eventsApplied"],2)
        self.assertFalse(x["edgeProven"])
        self.assertFalse(x["liveReady"])

    def test_gap_fails_closed(self):
        rows=[
            {"line":0,"prefix":"","data":{"lastUpdateId":100,"bids":[["99","10"]],"asks":[["101","10"]]}},
            {"line":1,"prefix":"","data":{"E":1000,"U":101,"u":101,"b":[["100","5"]],"a":[]}},
            {"line":2,"prefix":"","data":{"E":2000,"U":105,"u":105,"b":[],"a":[["102","2"]]}},
        ]
        x=replay(rows,"BTCUSDT",0,10_000,10)
        self.assertFalse(x["canonicalReplayReady"])
        self.assertEqual(x["status"],"DEPTH_GAP")


if __name__=="__main__":
    unittest.main()
