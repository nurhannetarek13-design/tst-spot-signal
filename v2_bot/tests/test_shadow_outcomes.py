import tempfile
import unittest
from pathlib import Path

from v2_bot.shadow_outcomes import ShadowOutcomeLedger


class ShadowOutcomeLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "shadow.sqlite3")
        self.ledger = ShadowOutcomeLedger(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def open(self, signal_open_time=1000.0):
        return self.ledger.open_signal(
            symbol="SOLUSDT",
            signal_open_time=signal_open_time,
            entry_price=100.0,
            quote_size=10.0,
            take_profit_pct=0.009,
            stop_loss_pct=0.0062,
            fee_rate=0.001,
        )

    def test_signal_candle_itself_is_never_used_for_forward_outcome(self):
        trade = self.open()
        result = self.ledger.evaluate_closed_candle(
            trade,
            {"open_time": 1000.0, "high": 102.0, "low": 98.0},
        )
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(len(self.ledger.open_outcomes()), 1)

    def test_tp_closes_with_fee_aware_positive_pnl(self):
        trade = self.open()
        result = self.ledger.evaluate_closed_candle(
            trade,
            {"open_time": 2000.0, "high": 101.0, "low": 99.8},
        )
        self.assertEqual(result.status, "TP")
        self.assertGreater(result.pnl_usdt, 0)
        stats = self.ledger.stats()
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 0)
        self.assertEqual(stats["win_rate"], 1.0)
        self.assertGreater(stats["expectancy_usdt"], 0)
        self.assertEqual(stats["gross_loss_abs_usdt"], 0.0)

    def test_sl_closes_with_fee_aware_negative_pnl(self):
        trade = self.open()
        result = self.ledger.evaluate_closed_candle(
            trade,
            {"open_time": 2000.0, "high": 100.2, "low": 99.0},
        )
        self.assertEqual(result.status, "SL")
        self.assertLess(result.pnl_usdt, 0)
        stats = self.ledger.stats()
        self.assertEqual(stats["wins"], 0)
        self.assertEqual(stats["losses"], 1)
        self.assertEqual(stats["win_rate"], 0.0)
        self.assertLess(stats["expectancy_usdt"], 0)
        self.assertEqual(stats["gross_profit_usdt"], 0.0)

    def test_both_tp_and_sl_same_candle_is_ambiguous_not_a_win(self):
        trade = self.open()
        result = self.ledger.evaluate_closed_candle(
            trade,
            {"open_time": 2000.0, "high": 101.5, "low": 98.5},
        )
        self.assertEqual(result.status, "AMBIGUOUS")
        self.assertIsNone(result.pnl_usdt)
        stats = self.ledger.stats()
        self.assertEqual(stats["ambiguous"], 1)
        self.assertEqual(stats["decisive"], 0)
        self.assertIsNone(stats["win_rate"])
        self.assertEqual(stats["net_pnl_usdt"], 0.0)
        self.assertEqual(stats["ambiguous_rate"], 1.0)

    def test_non_trigger_candle_leaves_trade_open(self):
        trade = self.open()
        result = self.ledger.evaluate_closed_candle(
            trade,
            {"open_time": 2000.0, "high": 100.5, "low": 99.5},
        )
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(self.ledger.stats()["open"], 1)

    def test_duplicate_signal_identity_is_rejected(self):
        self.open()
        with self.assertRaisesRegex(RuntimeError, "shadow_signal_already_tracked"):
            self.open()

    def test_fee_math_matches_expected_tp_net(self):
        trade = self.open()
        result = self.ledger.evaluate_closed_candle(
            trade,
            {"open_time": 2000.0, "high": 101.0, "low": 99.8},
        )
        # qty=.1, gross=.09; fees=.01 entry + .01009 exit = .02009
        self.assertAlmostEqual(result.pnl_usdt, 0.06991, places=8)

    def test_stats_expose_fee_aware_profit_factor_and_expectancy(self):
        winner = self.open(signal_open_time=1000.0)
        loser = self.open(signal_open_time=3000.0)
        self.ledger.evaluate_closed_candle(
            winner,
            {"open_time": 2000.0, "high": 101.0, "low": 99.8},
        )
        self.ledger.evaluate_closed_candle(
            loser,
            {"open_time": 4000.0, "high": 100.2, "low": 99.0},
        )

        stats = self.ledger.stats()
        self.assertEqual(stats["decisive"], 2)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 1)
        self.assertAlmostEqual(stats["gross_profit_usdt"], 0.06991, places=8)
        self.assertAlmostEqual(stats["gross_loss_abs_usdt"], 0.081938, places=8)
        self.assertLess(stats["profit_factor"], 1.0)
        self.assertLess(stats["expectancy_usdt"], 0.0)
        self.assertAlmostEqual(stats["net_pnl_usdt"], -0.012028, places=8)


if __name__ == "__main__":
    unittest.main()
