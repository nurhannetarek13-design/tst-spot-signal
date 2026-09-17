import unittest

from v2_bot.config import Settings
from v2_bot.runtime_engine import RuntimeV2Engine
from v2_bot.strategy_pool import (
    REJECTED,
    RESEARCH,
    REJECTED_SPECS,
    STRATEGY_SPECS,
    evaluate_pool,
    evaluate_strategy,
    registry_snapshot,
)


def candles(count=120, *, start=100.0, step=0.002, quote_volume=1_000_000.0, taker_ratio=0.60):
    rows = []
    price = start
    for index in range(count):
        open_price = price
        close_price = open_price * (1.0 + step)
        high = max(open_price, close_price) * 1.001
        low = min(open_price, close_price) * 0.999
        qv = quote_volume * (2.0 if index == count - 1 else 1.0)
        rows.append(
            {
                "open_time": float(index * 900_000),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close_price,
                "volume": qv / close_price,
                "quote_volume": qv,
                "taker_buy_quote": qv * taker_ratio,
                "close_time": float((index + 1) * 900_000 - 1),
            }
        )
        price = close_price
    return rows


class StrategyRegistryTests(unittest.TestCase):
    def test_rejected_legacy_families_cannot_silently_reenter_pool(self):
        ids = {spec.strategy_id for spec in REJECTED_SPECS}
        self.assertEqual(
            ids,
            {
                "strict_current",
                "breakout_continuation",
                "volatility_expansion",
                "htf_pullback_reclaim",
            },
        )
        self.assertTrue(all(spec.status == REJECTED for spec in REJECTED_SPECS))

    def test_new_families_start_research_only(self):
        self.assertGreaterEqual(len(STRATEGY_SPECS), 8)
        self.assertTrue(all(spec.status == RESEARCH for spec in STRATEGY_SPECS))
        snapshot = registry_snapshot()
        self.assertEqual(len(snapshot), len(REJECTED_SPECS) + len(STRATEGY_SPECS))


class StrategyRoutingTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            min_quote_volume_24h=20_000_000.0,
            max_spread_bps=15.0,
        )
        self.c15 = candles(step=0.0020)
        self.c1 = candles(step=0.0050)
        self.c4 = candles(step=0.0040)
        self.btc1 = candles(step=0.0050)

    def test_research_signal_is_diagnostic_in_paper_not_executable(self):
        trend = next(spec for spec in STRATEGY_SPECS if spec.strategy_id == "trend_momentum")
        result = evaluate_strategy(
            trend,
            candles_15m=self.c15,
            candles_1h=self.c1,
            candles_4h=self.c4,
            btc_1h=self.btc1,
            spread_bps=1.0,
            quote_volume_24h=100_000_000.0,
            settings=self.settings,
            mode="paper",
        )
        self.assertTrue(result.signal_ok)
        self.assertFalse(result.executable)
        self.assertIn("strategy_status_research_not_executable_in_paper", result.failed_gates)

    def test_same_research_signal_can_be_observed_in_shadow(self):
        trend = next(spec for spec in STRATEGY_SPECS if spec.strategy_id == "trend_momentum")
        result = evaluate_strategy(
            trend,
            candles_15m=self.c15,
            candles_1h=self.c1,
            candles_4h=self.c4,
            btc_1h=self.btc1,
            spread_bps=1.0,
            quote_volume_24h=100_000_000.0,
            settings=self.settings,
            mode="shadow",
        )
        self.assertTrue(result.signal_ok)
        self.assertTrue(result.executable)

    def test_pool_carries_strategy_and_exit_metadata(self):
        pool = evaluate_pool(
            symbol="TESTUSDT",
            candles_15m=self.c15,
            candles_1h=self.c1,
            candles_4h=self.c4,
            btc_1h=self.btc1,
            spread_bps=1.0,
            quote_volume_24h=100_000_000.0,
            settings=self.settings,
            mode="paper",
        )
        self.assertEqual(len(pool), len(STRATEGY_SPECS))
        trend = next(c for c in pool if c.strategy_id == "trend_momentum")
        self.assertEqual(trend.strategy_status, RESEARCH)
        self.assertTrue(trend.strategy_signal_ok)
        self.assertFalse(trend.eligible)
        self.assertGreater(trend.take_profit_pct, 0)
        self.assertGreater(trend.stop_loss_pct, 0)

    def test_runtime_failed_gate_diagnostics_use_strategy_specific_gates(self):
        pool = evaluate_pool(
            symbol="TESTUSDT",
            candles_15m=self.c15,
            candles_1h=self.c1,
            candles_4h=self.c4,
            btc_1h=self.btc1,
            spread_bps=1.0,
            quote_volume_24h=100_000_000.0,
            settings=self.settings,
            mode="paper",
        )
        trend = next(c for c in pool if c.strategy_id == "trend_momentum")
        failed = RuntimeV2Engine._failed_gates(trend, self.settings)
        self.assertEqual(failed, ["strategy_status_research_not_executable_in_paper"])


if __name__ == "__main__":
    unittest.main()
