import math
import unittest

from research.canonical_features import (
    amihud_illiquidity,
    beta,
    bns_jump_statistic,
    correlation,
    cusum_vol_adaptive,
    log_returns,
    realized_volatility,
    skewness_excess_kurtosis,
)


class CanonicalFeatureTests(unittest.TestCase):
    def test_log_returns(self):
        prices = [100.0, 101.0, 102.01]
        out = log_returns(prices)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0], math.log(1.01), places=12)
        self.assertAlmostEqual(out[1], math.log(1.01), places=12)

    def test_beta_and_correlation_identity(self):
        r = [0.01, -0.02, 0.03, 0.015, -0.01]
        self.assertAlmostEqual(beta(r, r), 1.0, places=12)
        self.assertAlmostEqual(correlation(r, r), 1.0, places=12)

    def test_beta_scales_with_asset_sensitivity(self):
        benchmark = [0.01, -0.02, 0.03, 0.015, -0.01]
        asset = [2.0 * x for x in benchmark]
        self.assertAlmostEqual(beta(asset, benchmark), 2.0, places=12)

    def test_skew_kurtosis_is_finite(self):
        skew, ex_kurt = skewness_excess_kurtosis([-0.02, -0.01, 0.0, 0.01, 0.03, 0.08])
        self.assertTrue(math.isfinite(skew))
        self.assertTrue(math.isfinite(ex_kurt))

    def test_amihud_uses_quote_volume(self):
        closes = [100, 101, 100, 102, 103, 104]
        quote_vol = [1_000_000] * len(closes)
        out = amihud_illiquidity(closes, quote_vol, window=2)
        self.assertIsNone(out[0])
        self.assertGreater(out[-1], 0.0)

    def test_realized_volatility_positive(self):
        vol = realized_volatility([100, 101, 99, 102, 101, 104])
        self.assertGreater(vol, 0.0)

    def test_cusum_detects_large_break(self):
        prices = [100.0]
        for _ in range(30):
            prices.append(prices[-1] * 1.0001)
        prices.append(prices[-1] * 1.10)
        events = cusum_vol_adaptive(prices, vol_lookback=10, threshold_mult=1.0, min_bar_gap=1)
        self.assertTrue(any(e.direction == 1 for e in events))

    def test_bns_result_is_bounded(self):
        prices = [100.0]
        for i in range(40):
            step = 1.001 if i % 2 == 0 else 0.999
            if i == 35:
                step = 1.08
            prices.append(prices[-1] * step)
        result = bns_jump_statistic(prices, window=22)
        self.assertGreaterEqual(result.relative_jump, 0.0)
        self.assertLessEqual(result.relative_jump, 1.0)
        self.assertIn(result.regime, {"CONTINUOUS", "MIXED", "JUMP"})


if __name__ == "__main__":
    unittest.main()
