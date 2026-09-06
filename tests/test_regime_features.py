import math
import random
import unittest

from research.regime_features import (
    fractional_difference,
    fractional_diff_weights,
    garch11_fit_forecast,
    hurst_rs,
    shannon_entropy_returns,
    variance_ratio,
    variance_ratio_regime,
)


class RegimeFeatureTests(unittest.TestCase):
    def test_fractional_diff_weights_decay(self):
        w = fractional_diff_weights(0.3, threshold=1e-4, max_width=500)
        self.assertGreater(len(w), 2)
        self.assertAlmostEqual(w[0], 1.0)
        self.assertTrue(all(math.isfinite(v) for v in w))
        self.assertLess(abs(w[-1]), abs(w[1]))

    def test_fractional_difference_shape(self):
        x = [100.0 + i for i in range(300)]
        out = fractional_difference(x, d=0.3, threshold=1e-3)
        self.assertEqual(len(out), len(x))
        valid = [v for v in out if v is not None]
        self.assertTrue(valid)
        self.assertTrue(all(math.isfinite(v) for v in valid))

    def test_entropy_bounds(self):
        prices = [100.0]
        for i in range(100):
            prices.append(prices[-1] * (1.001 if i % 2 == 0 else 0.999))
        h = shannon_entropy_returns(prices, window=64, bins=8)
        self.assertGreaterEqual(h, 0.0)
        self.assertLessEqual(h, 1.0)

    def test_variance_ratio_trend_series(self):
        returns = [0.001 + 0.00002 * i for i in range(100)]
        vr = variance_ratio(returns, 4)
        self.assertGreater(vr, 1.0)

    def test_variance_ratio_regime_returns_valid_state(self):
        prices = [100.0]
        for i in range(200):
            prices.append(prices[-1] * (1.001 + 0.00001 * (i % 7)))
        result = variance_ratio_regime(prices, horizons=(2, 4, 8, 16), votes_required=3)
        self.assertIn(result.regime, {"TREND", "MEAN_REVERT", "RANDOM"})
        self.assertEqual(set(result.ratios), {2, 4, 8, 16})

    def test_hurst_is_finite_on_persistent_path(self):
        prices = [100.0]
        for i in range(700):
            step = 0.0008 + 0.0002 * math.sin(i / 20)
            prices.append(prices[-1] * math.exp(step))
        h = hurst_rs(prices, sample_sizes=(32, 64, 128, 256))
        self.assertTrue(math.isfinite(h))
        self.assertGreater(h, 0.0)
        self.assertLess(h, 1.5)

    def test_garch_forecast_positive_and_stationary(self):
        random.seed(7)
        prices = [100.0]
        for i in range(250):
            sigma = 0.004 if i < 120 else 0.012
            r = random.gauss(0.0, sigma)
            prices.append(prices[-1] * math.exp(r))
        fit = garch11_fit_forecast(prices, horizon=5)
        self.assertGreater(fit.forecast_variance, 0.0)
        self.assertGreater(fit.forecast_volatility, 0.0)
        self.assertLess(fit.alpha + fit.beta, 1.0)
        self.assertTrue(math.isfinite(fit.objective))


if __name__ == "__main__":
    unittest.main()
