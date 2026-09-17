import tempfile
import unittest
from pathlib import Path

from v2_bot.shadow_outcomes import ShadowOutcomeLedger
from v2_bot.shadow_tournament import (
    ShadowPromotionPolicy,
    StrategyShadowLedgerAdapter,
    decode_shadow_key,
    encode_shadow_key,
    evaluate_shadow_promotion,
    first_promotion_candidate,
    rank_shadow_strategies,
)


class ShadowTournamentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "shadow.sqlite3")
        self.inner = ShadowOutcomeLedger(self.db)
        self.ledger = StrategyShadowLedgerAdapter(self.inner)

    def tearDown(self):
        self.tmp.cleanup()

    def _open(self, strategy_id, signal_open_time=1000.0):
        return self.ledger.open_signal(
            strategy_id=strategy_id,
            symbol="SOLUSDT",
            signal_open_time=signal_open_time,
            entry_price=100.0,
            quote_size=10.0,
            take_profit_pct=0.009,
            stop_loss_pct=0.0062,
            fee_rate=0.001,
        )

    def test_shadow_key_round_trip(self):
        key = encode_shadow_key("solusdt", "trend_momentum")
        self.assertEqual(key, "SOLUSDT::trend_momentum")
        self.assertEqual(decode_shadow_key(key), ("SOLUSDT", "trend_momentum"))
        self.assertEqual(decode_shadow_key("BTCUSDT"), ("BTCUSDT", None))

    def test_same_symbol_and_candle_can_be_tracked_by_multiple_strategies(self):
        a = self._open("trend_momentum")
        b = self._open("range_reversion")
        self.assertNotEqual(a.symbol, b.symbol)
        self.assertEqual(len(self.ledger.open_outcomes()), 2)

    def test_stats_are_isolated_by_strategy(self):
        winner = self._open("trend_momentum", 1000.0)
        loser = self._open("range_reversion", 2000.0)
        self.ledger.evaluate_closed_candle(
            winner,
            {"open_time": 3000.0, "high": 101.0, "low": 99.8},
        )
        self.ledger.evaluate_closed_candle(
            loser,
            {"open_time": 3000.0, "high": 100.2, "low": 99.0},
        )
        stats = self.ledger.stats_by_strategy()
        self.assertEqual(stats["trend_momentum"]["wins"], 1)
        self.assertEqual(stats["trend_momentum"]["losses"], 0)
        self.assertEqual(stats["range_reversion"]["wins"], 0)
        self.assertEqual(stats["range_reversion"]["losses"], 1)

    def test_promotion_requires_forward_sample_profit_factor_and_positive_expectancy(self):
        policy = ShadowPromotionPolicy()
        good = {
            "decisive": 60,
            "wins": 40,
            "losses": 20,
            "profit_factor": 1.35,
            "expectancy_usdt": 0.02,
            "net_pnl_usdt": 1.2,
            "ambiguous_rate": 0.05,
        }
        passed, blockers = evaluate_shadow_promotion(good, policy)
        self.assertTrue(passed)
        self.assertEqual(blockers, [])

        too_small = dict(good, decisive=12, wins=12, losses=0, profit_factor=None)
        passed, blockers = evaluate_shadow_promotion(too_small, policy)
        self.assertFalse(passed)
        self.assertIn("shadow_sample_insufficient", blockers)
        self.assertIn("shadow_profit_factor_unproven", blockers)

    def test_first_promotion_candidate_comes_from_ranked_forward_evidence(self):
        policy = ShadowPromotionPolicy()
        stats = {
            "trend_momentum": {
                "decisive": 60,
                "wins": 38,
                "losses": 22,
                "profit_factor": 1.25,
                "expectancy_usdt": 0.01,
                "net_pnl_usdt": 0.6,
                "ambiguous_rate": 0.02,
            },
            "range_reversion": {
                "decisive": 60,
                "wins": 30,
                "losses": 30,
                "profit_factor": 0.9,
                "expectancy_usdt": -0.01,
                "net_pnl_usdt": -0.6,
                "ambiguous_rate": 0.01,
            },
        }
        rankings = rank_shadow_strategies(stats, policy)
        self.assertEqual(rankings[0]["strategy_id"], "trend_momentum")
        self.assertTrue(rankings[0]["promotion_ready"])
        self.assertEqual(first_promotion_candidate(rankings), "trend_momentum")


if __name__ == "__main__":
    unittest.main()
