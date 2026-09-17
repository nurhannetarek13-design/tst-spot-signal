import unittest

from v2_bot.external_btc_edge import (
    STRATEGY_ID,
    default_state,
    evaluate_snapshot,
    process_cycle,
)


HOUR_MS = 3_600_000
BASE_MS = 1_787_000_000_000 - 601 * HOUR_MS


def make_cross_candles():
    candles = []
    for i in range(601):
        close = 100.0
        candles.append(
            {
                "open_time": float(BASE_MS + i * HOUR_MS),
                "close_time": float(BASE_MS + (i + 1) * HOUR_MS - 1),
                "open": close,
                "high": close * 1.001,
                "low": close * 0.999,
                "close": close,
            }
        )
    candles[-2].update(close=99.0, high=99.2, low=98.8)
    candles[-1].update(close=101.0, high=101.2, low=100.8)
    return candles


def make_funding(candles, rate=0.00001):
    end_ms = int(candles[-1]["close_time"])
    start_ms = end_ms - 200 * 24 * HOUR_MS
    rows = []
    timestamp = start_ms
    while timestamp <= end_ms:
        rows.append({"fundingTime": timestamp, "fundingRate": rate})
        timestamp += 8 * HOUR_MS
    return rows


class ExternalBtcEdgeTests(unittest.TestCase):
    def test_low_funding_cross_is_allowed(self):
        candles = make_cross_candles()
        snapshot = evaluate_snapshot(candles, make_funding(candles))
        self.assertTrue(snapshot.crossed_above)
        self.assertTrue(snapshot.funding_allowed)
        self.assertAlmostEqual(snapshot.funding_percentile, 50.0)

    def test_frothy_funding_blocks_entry(self):
        candles = make_cross_candles()
        funding = make_funding(candles)
        for row in funding[-9:]:
            row["fundingRate"] = 0.001
        snapshot = evaluate_snapshot(candles, funding)
        self.assertTrue(snapshot.crossed_above)
        self.assertFalse(snapshot.funding_allowed)
        self.assertGreater(snapshot.funding_percentile, 55.0)

    def test_missing_funding_fails_closed(self):
        candles = make_cross_candles()
        snapshot = evaluate_snapshot(candles, [])
        self.assertFalse(snapshot.funding_allowed)
        self.assertIsNone(snapshot.funding_percentile)

    def test_shadow_open_is_research_only_and_idempotent_per_candle(self):
        candles = make_cross_candles()
        funding = make_funding(candles)
        state, result = process_cycle(
            state=default_state(),
            candles=candles,
            funding_rows=funding,
            quote_size_usdt=10.0,
            entry_ask=101.05,
        )
        self.assertEqual(result["event"]["type"], "shadow_open")
        self.assertFalse(result["promotion_eligible"])
        self.assertFalse(result["live_eligible"])
        self.assertEqual(result["strategy_id"], STRATEGY_ID)

        state, duplicate = process_cycle(
            state=state,
            candles=candles,
            funding_rows=funding,
            quote_size_usdt=10.0,
            entry_ask=101.05,
        )
        self.assertIsNone(duplicate["event"])
        self.assertEqual(duplicate["stats"]["closed"], 0)

    def test_trailing_exit_records_net_loss(self):
        candles = make_cross_candles()
        funding = make_funding(candles)
        state, opened = process_cycle(
            state=default_state(),
            candles=candles,
            funding_rows=funding,
            quote_size_usdt=10.0,
            entry_ask=101.05,
        )
        self.assertEqual(opened["event"]["type"], "shadow_open")

        next_candles = list(candles)
        next_candles.append(
            {
                "open_time": candles[-1]["open_time"] + HOUR_MS,
                "close_time": candles[-1]["close_time"] + HOUR_MS,
                "open": 100.0,
                "high": 101.0,
                "low": 80.0,
                "close": 90.0,
            }
        )
        state, closed = process_cycle(
            state=state,
            candles=next_candles,
            funding_rows=funding,
            quote_size_usdt=10.0,
            exit_bid=89.9,
        )
        self.assertEqual(closed["event"]["type"], "shadow_close")
        self.assertEqual(closed["event"]["reason"], "trailing_stop")
        self.assertLess(closed["event"]["pnl_usdt_net_fees"], 0)
        self.assertEqual(closed["stats"]["closed"], 1)
        self.assertEqual(closed["stats"]["losses"], 1)
        self.assertFalse(closed["stats"]["open"])


if __name__ == "__main__":
    unittest.main()
