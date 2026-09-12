import tempfile
import unittest
from pathlib import Path

from v2_bot.state import StateStore
from v2_bot.strategy_state import StrategyAwareStateProxy


class StrategyAwareStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "state.sqlite3")
        self.proxy = StrategyAwareStateProxy(StateStore(self.path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_prepared_profile_overrides_legacy_exit_percentages(self):
        self.proxy.prepare_profile(
            symbol="BTCUSDT",
            strategy_id="trend_momentum",
            take_profit_pct=0.012,
            stop_loss_pct=0.0075,
        )
        position = self.proxy.open_position(
            symbol="BTCUSDT",
            entry_price=100.0,
            quote_size=10.0,
            take_profit_pct=0.009,
            stop_loss_pct=0.0062,
        )
        self.assertAlmostEqual(position.take_profit, 101.2)
        self.assertAlmostEqual(position.stop_loss, 99.25)
        self.assertEqual(self.proxy.strategy_for_position(position), "trend_momentum")

    def test_strategy_identity_survives_proxy_recreation(self):
        self.proxy.prepare_profile(
            symbol="ETHUSDT",
            strategy_id="range_reversion",
            take_profit_pct=0.0065,
            stop_loss_pct=0.005,
        )
        position = self.proxy.open_position(
            symbol="ETHUSDT",
            entry_price=200.0,
            quote_size=10.0,
            take_profit_pct=0.009,
            stop_loss_pct=0.0062,
        )
        recreated = StrategyAwareStateProxy(StateStore(self.path))
        stored = recreated.list_open_positions()[0]
        self.assertEqual(stored.symbol, position.symbol)
        self.assertEqual(recreated.strategy_for_position(stored), "range_reversion")

    def test_close_qualifies_trade_reason_with_strategy(self):
        self.proxy.prepare_profile(
            symbol="SOLUSDT",
            strategy_id="btc_alt_leadlag",
            take_profit_pct=0.010,
            stop_loss_pct=0.0065,
        )
        position = self.proxy.open_position(
            symbol="SOLUSDT",
            entry_price=100.0,
            quote_size=10.0,
            take_profit_pct=0.009,
            stop_loss_pct=0.0062,
        )
        self.proxy.close_position(
            position,
            exit_price=101.0,
            reason="take_profit",
            fee_rate=0.001,
        )
        import sqlite3
        with sqlite3.connect(self.path) as conn:
            reason = conn.execute("SELECT reason FROM trades").fetchone()[0]
        self.assertEqual(reason, "btc_alt_leadlag:take_profit")


if __name__ == "__main__":
    unittest.main()
