import unittest

from research.validation_pipeline import (
    aggregate_fold_metric,
    apply_embargo,
    equity_curve_from_returns,
    max_drawdown_fraction,
    monte_carlo_trade_paths,
    oos_degradation,
    walk_forward_folds,
)


class ValidationPipelineTests(unittest.TestCase):
    def test_walk_forward_rolling_and_purged(self):
        folds = walk_forward_folds(1000, 400, 100, purge=24, embargo=12)
        self.assertGreater(len(folds), 1)
        first = folds[0]
        self.assertEqual(first.train_end + 24, first.test_start)
        self.assertEqual(first.test_end - first.test_start, 100)
        self.assertGreaterEqual(first.train_start, 0)

    def test_walk_forward_anchored(self):
        folds = walk_forward_folds(800, 300, 100, anchored=True)
        self.assertTrue(all(f.train_start == 0 for f in folds))
        self.assertGreater(folds[-1].train_end, folds[0].train_end)

    def test_embargo(self):
        out = apply_embargo([1, 9, 10, 15, 20, 25], 10, 20, 5)
        self.assertEqual(out, [1, 9, 25])

    def test_equity_and_drawdown(self):
        eq = equity_curve_from_returns([0.10, -0.10, 0.05], 100.0)
        self.assertEqual(len(eq), 3)
        self.assertGreater(max_drawdown_fraction(eq), 0.0)

    def test_monte_carlo_is_seed_deterministic(self):
        returns = [0.03, -0.01, 0.02, -0.015, 0.04, 0.005]
        a = monte_carlo_trade_paths(returns, simulations=200, seed=11)
        b = monte_carlo_trade_paths(returns, simulations=200, seed=11)
        self.assertEqual(a, b)
        self.assertGreater(a.median_final_equity, 0.0)
        self.assertGreaterEqual(a.ruin_probability, 0.0)
        self.assertLessEqual(a.ruin_probability, 1.0)

    def test_degradation(self):
        self.assertAlmostEqual(oos_degradation(2.0, 1.5), 0.25)

    def test_fold_metric(self):
        x = aggregate_fold_metric([1.0, 2.0, 3.0])
        self.assertEqual(x['median'], 2.0)
        self.assertEqual(x['worst'], 1.0)


if __name__ == '__main__':
    unittest.main()
