import unittest

from v2_bot.config import Settings
from v2_bot.risk import check_risk
from v2_bot.strategy import Candidate


def candidate(score=100, eligible=True):
    return Candidate(
        symbol="TESTUSDT",
        score=score,
        price=1.0,
        previous_20_high=0.99,
        relative_volume=2.0,
        taker_buy_ratio=0.60,
        spread_bps=5.0,
        quote_volume_24h=100_000_000.0,
        btc_regime_ok=True,
        trend_15m=True,
        trend_1h=True,
        trend_4h=True,
        breakout=True,
        rel_volume_ok=True,
        taker_flow_ok=True,
        eligible=eligible,
    )


class RiskTests(unittest.TestCase):
    def test_blocks_at_daily_loss_cap(self):
        settings = Settings()
        result = check_risk(
            settings=settings,
            candidate=candidate(),
            realized_pnl_today=-settings.max_daily_loss_usdt,
            open_positions=0,
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "daily_loss_limit_reached")

    def test_allows_clean_candidate(self):
        settings = Settings()
        result = check_risk(
            settings=settings,
            candidate=candidate(),
            realized_pnl_today=0.0,
            open_positions=0,
        )
        self.assertTrue(result.allowed)


if __name__ == "__main__":
    unittest.main()
