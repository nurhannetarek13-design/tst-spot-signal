import unittest

from research.microstructure_replay_validator import replay


def base_events():
    return [
        {
            "type":"depth_snapshot","symbol":"SOLUSDT","ts_ms":1000,"last_update_id":100,
            "bids":[["99.99","10"]],"asks":[["100.00","10"],["100.01","10"]],
        },
        {"type":"agg_trade","symbol":"SOLUSDT","ts_ms":1200,"price":"100","qty":"1","buyer_maker":False},
        {"type":"signal","symbol":"SOLUSDT","ts_ms":1500},
        {"type":"mark","symbol":"SOLUSDT","ts_ms":902000,"price":101.0},
    ]


class ReplayTests(unittest.TestCase):
    def test_replay_includes_fees_latency_and_depth_slippage(self):
        r=replay(
            base_events(),
            initial_equity=1000,
            quote_per_trade=10,
            fee_rate=.001,
            latency_ms=500,
            horizon_ms=900_000,
            universe_source="HISTORICAL_ALL_LISTED_INCLUDING_DELISTED",
            survivorship_bias_checked=True,
        )
        self.assertEqual(r["trades"],1)
        self.assertTrue(r["survivorship_bias_checked"])
        self.assertEqual(r["lookahead_violations"],0)
        fill=[x for x in r["decisions"] if x["code"]=="FILLED"][0]
        self.assertLess(fill["net_return_after_fees"],fill["gross_return"])
        self.assertGreaterEqual(fill["slippage_bps"],0)

    def test_stale_book_is_fail_closed(self):
        ev=base_events()
        ev[2]["ts_ms"]=20_000
        ev[-1]["ts_ms"]=920_000
        r=replay(ev,latency_ms=500,max_book_age_ms=3000,horizon_ms=900_000)
        self.assertEqual(r["trades"],0)
        self.assertGreater(r["stale_rejections"],0)

    def test_depth_gap_blocks_signal(self):
        ev=[
            {"type":"depth_snapshot","symbol":"SOLUSDT","ts_ms":1000,"last_update_id":100,
             "bids":[["99","10"]],"asks":[["100","10"]]},
            {"type":"depth_update","symbol":"SOLUSDT","ts_ms":1100,"first_update_id":105,"final_update_id":106,
             "bids":[],"asks":[]},
            {"type":"signal","symbol":"SOLUSDT","ts_ms":1300},
            {"type":"mark","symbol":"SOLUSDT","ts_ms":901300,"price":101},
        ]
        r=replay(ev,horizon_ms=900_000)
        self.assertEqual(r["trades"],0)
        self.assertGreater(r["gap_rejections"],0)


    def test_latency_uses_book_state_as_of_execution_deadline(self):
        ev=[
            {"type":"depth_snapshot","symbol":"SOLUSDT","ts_ms":1000,"last_update_id":100,
             "bids":[["99.9","10"]],"asks":[["100.0","10"]]},
            {"type":"signal","symbol":"SOLUSDT","ts_ms":1500},
            {"type":"depth_update","symbol":"SOLUSDT","ts_ms":1900,"first_update_id":101,"final_update_id":101,
             "bids":[["99.9","10"]],"asks":[["100.0","0"],["101.0","10"]]},
            {"type":"depth_update","symbol":"SOLUSDT","ts_ms":2100,"first_update_id":102,"final_update_id":102,
             "bids":[["99.9","10"]],"asks":[["101.0","0"],["90.0","10"]]},
            {"type":"mark","symbol":"SOLUSDT","ts_ms":902000,"price":102.0},
        ]
        r=replay(ev,latency_ms=500,horizon_ms=900_000,max_book_age_ms=3000)
        fill=[x for x in r["decisions"] if x["code"]=="FILLED"][0]
        self.assertAlmostEqual(fill["fill_price"],101.0)
        self.assertEqual(fill["exec_ts_ms"],2000)
        self.assertEqual(fill["book_ts_ms"],1900)
        self.assertEqual(r["lookahead_violations"],0)

    def test_snapshot_resync_clears_prior_gap(self):
        ev=[
            {"type":"depth_snapshot","symbol":"SOLUSDT","ts_ms":1000,"last_update_id":100,
             "bids":[["99","10"]],"asks":[["100","10"]]},
            {"type":"depth_update","symbol":"SOLUSDT","ts_ms":1100,"first_update_id":105,"final_update_id":106,
             "bids":[],"asks":[]},
            {"type":"depth_snapshot","symbol":"SOLUSDT","ts_ms":1200,"last_update_id":200,
             "bids":[["99.5","10"]],"asks":[["100","10"]]},
            {"type":"signal","symbol":"SOLUSDT","ts_ms":1300},
            {"type":"mark","symbol":"SOLUSDT","ts_ms":901800,"price":101},
        ]
        r=replay(ev,latency_ms=500,horizon_ms=900_000,max_book_age_ms=3000)
        self.assertEqual(r["trades"],1)
        self.assertEqual(r["lookahead_violations"],0)


if __name__=="__main__":
    unittest.main()
