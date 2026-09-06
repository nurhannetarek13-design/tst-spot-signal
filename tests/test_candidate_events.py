import unittest

from research.candidate_events import (
    breadth_lead_lag_continuation,
    canonical_candidate_set,
    liquidity_crash_exhaustion,
    residual_dispersion_reversal,
)


class CandidateEventTests(unittest.TestCase):
    def setUp(self):
        self.features = {
            "residual_z": [-1.0, -3.4, -3.2],
            "bns_relative_jump": [0.05, 0.35, 0.10],
            "amihud_z": [0.0, 2.1, 0.5],
            "aggressor_imbalance": [0.1, -0.35, -0.10],
            "aggressor_imbalance_delta": [0.0, 0.10, 0.20],
            "entropy": [0.95, 0.70, 0.75],
            "hurst": [0.60, 0.50, 0.45],
            "breadth_positive_share": [0.40, 0.70, 0.80],
            "btc_lead_return": [0.0, 0.015, 0.020],
            "relative_strength_btc": [0.02, -0.03, -0.02],
            "taker_buy_share": [0.50, 0.60, 0.58],
            "oi_change_pct": [-0.01, 0.02, 0.01],
        }

    def test_liquidity_crash_event(self):
        result = liquidity_crash_exhaustion(self.features)
        self.assertEqual(result.indices, (1,))
        self.assertEqual(result.definition.direction, 1)

    def test_residual_reversal_events(self):
        result = residual_dispersion_reversal(self.features)
        self.assertEqual(result.indices, (1, 2))

    def test_breadth_lead_lag_events(self):
        result = breadth_lead_lag_continuation(self.features)
        self.assertEqual(result.indices, (1, 2))

    def test_canonical_candidate_set_contains_three_priority_families(self):
        candidates = canonical_candidate_set(self.features)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(len({c.definition.name for c in candidates}), 3)

    def test_alignment_is_enforced(self):
        broken = dict(self.features)
        broken["hurst"] = [0.5]
        with self.assertRaises(ValueError):
            residual_dispersion_reversal(broken)


if __name__ == "__main__":
    unittest.main()
