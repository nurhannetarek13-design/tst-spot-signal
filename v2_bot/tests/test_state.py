import tempfile
import unittest
from pathlib import Path

from v2_bot.state import StateStore


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "state.sqlite3")
        self.state = StateStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_position_open_close_and_net_fee_pnl(self):
        position = self.state.open_position(
            symbol="TESTUSDT",
            entry_price=100.0,
            quote_size=10.0,
            take_profit_pct=0.01,
            stop_loss_pct=0.01,
        )
        self.assertEqual(len(self.state.list_open_positions()), 1)
        self.assertAlmostEqual(position.quantity, 0.1)
        self.assertAlmostEqual(position.take_profit, 101.0)
        self.assertAlmostEqual(position.stop_loss, 99.0)

        pnl = self.state.close_position(
            position,
            exit_price=101.0,
            reason="take_profit",
            fee_rate=0.001,
        )
        # Gross = +0.10 USDT; fees = 0.010 + 0.0101 USDT.
        self.assertAlmostEqual(pnl, 0.0799, places=6)
        self.assertEqual(self.state.list_open_positions(), [])
        self.assertAlmostEqual(self.state.realized_pnl_today(), 0.0799, places=6)

    def test_duplicate_position_is_rejected_not_replaced(self):
        first = self.state.open_position(
            symbol="TESTUSDT",
            entry_price=100.0,
            quote_size=10.0,
            take_profit_pct=0.01,
            stop_loss_pct=0.01,
        )
        with self.assertRaisesRegex(RuntimeError, "position_already_open"):
            self.state.open_position(
                symbol="TESTUSDT",
                entry_price=200.0,
                quote_size=10.0,
                take_profit_pct=0.01,
                stop_loss_pct=0.01,
            )

        current = self.state.list_open_positions()[0]
        self.assertEqual(current.opened_at, first.opened_at)
        self.assertEqual(current.entry_price, 100.0)

    def test_stale_position_cannot_be_closed_twice(self):
        position = self.state.open_position(
            symbol="TESTUSDT",
            entry_price=100.0,
            quote_size=10.0,
            take_profit_pct=0.01,
            stop_loss_pct=0.01,
        )
        self.state.close_position(position, exit_price=101.0, reason="take_profit", fee_rate=0.001)
        with self.assertRaisesRegex(RuntimeError, "position_not_open_or_stale"):
            self.state.close_position(position, exit_price=101.0, reason="take_profit", fee_rate=0.001)

    def test_signal_claim_is_unique_per_symbol_candle_and_kind(self):
        self.assertTrue(self.state.claim_signal(symbol="TESTUSDT", signal_open_time=123.0, kind="shadow"))
        self.assertFalse(self.state.claim_signal(symbol="TESTUSDT", signal_open_time=123.0, kind="shadow"))
        self.assertTrue(self.state.claim_signal(symbol="TESTUSDT", signal_open_time=124.0, kind="shadow"))
        self.assertTrue(self.state.claim_signal(symbol="TESTUSDT", signal_open_time=123.0, kind="paper"))

    def test_persistence_probe_first_revision_is_not_proven(self):
        probe = self.state.verify_persistence("rev-a")
        self.assertFalse(probe.proven)
        self.assertEqual(probe.reason, "first_deploy_marker_created")
        self.assertIsNone(probe.previous_revision)
        self.assertEqual(probe.current_revision, "rev-a")

    def test_persistence_probe_same_revision_is_not_cross_deploy_proof(self):
        self.state.verify_persistence("rev-a")
        probe = self.state.verify_persistence("rev-a")
        self.assertFalse(probe.proven)
        self.assertEqual(probe.reason, "same_deploy_revision_not_cross_deploy_proof")
        self.assertEqual(probe.previous_revision, "rev-a")

    def test_persistence_probe_is_proven_after_reopen_with_new_revision(self):
        self.state.verify_persistence("rev-a")
        reopened = StateStore(self.db_path)
        probe = reopened.verify_persistence("rev-b")
        self.assertTrue(probe.proven)
        self.assertEqual(probe.reason, "survived_prior_deployment")
        self.assertEqual(probe.previous_revision, "rev-a")
        self.assertEqual(probe.current_revision, "rev-b")

    def test_persistence_probe_rejects_missing_revision(self):
        probe = self.state.verify_persistence("")
        self.assertFalse(probe.proven)
        self.assertEqual(probe.reason, "deploy_revision_missing")


if __name__ == "__main__":
    unittest.main()
