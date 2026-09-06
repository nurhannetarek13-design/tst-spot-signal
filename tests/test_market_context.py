import unittest

from research.market_context import (
    ablation_delta,
    forward_returns,
    open_interest_delta,
    order_flow_from_aggressor_volume,
    relative_strength,
    spot_perp_context,
    spot_perp_cvd_divergence,
    triple_barrier_label,
)


class MarketContextTests(unittest.TestCase):
    def test_order_flow_buy_share(self):
        r = order_flow_from_aggressor_volume([60, 70], [40, 30], window=2)
        self.assertGreater(r.delta, 0)
        self.assertAlmostEqual(r.buy_share, 0.65)
        self.assertAlmostEqual(r.imbalance, 0.30)

    def test_open_interest_delta(self):
        d, pct = open_interest_delta([100, 110])
        self.assertEqual(d, 10)
        self.assertAlmostEqual(pct, 0.10)

    def test_spot_perp_context(self):
        ctx = spot_perp_context([100, 101], [100, 102], [1000, 1050])
        self.assertGreater(ctx.basis_pct, 0)
        self.assertGreater(ctx.oi_change_pct, 0)
        self.assertLess(ctx.spot_perp_return_spread, 0)

    def test_cvd_divergence(self):
        d = spot_perp_cvd_divergence([10, 10, 10], [-10, -10, -10], window=3)
        self.assertGreater(d, 0)

    def test_relative_strength(self):
        a = [100, 102, 104, 108]
        b = [100, 101, 102, 103]
        self.assertGreater(relative_strength(a, b, lookback=3), 0)

    def test_triple_barrier_stop_first_tie(self):
        c = [100, 100, 100]
        h = [100, 103, 100]
        l = [100, 97, 100]
        label = triple_barrier_label(c, h, l, entry_index=0, direction=1, pt_pct=0.02, sl_pct=0.02, max_hold=2)
        self.assertEqual(label.outcome, -1)
        self.assertEqual(label.exit_index, 1)

    def test_triple_barrier_profit(self):
        c = [100, 101, 102]
        h = [100, 103, 103]
        l = [100, 99.5, 101]
        label = triple_barrier_label(c, h, l, entry_index=0, direction=1, pt_pct=0.02, sl_pct=0.02, max_hold=2)
        self.assertEqual(label.outcome, 1)
        self.assertAlmostEqual(label.return_pct, 0.02)

    def test_forward_returns(self):
        out = forward_returns([100, 101, 102, 104], [1, 2])
        self.assertAlmostEqual(out[1][0], 0.01)
        self.assertAlmostEqual(out[2][0], 0.02)
        self.assertIsNone(out[2][-1])

    def test_ablation_delta(self):
        self.assertAlmostEqual(ablation_delta(1.10, 1.25), 0.15)


if __name__ == "__main__":
    unittest.main()
