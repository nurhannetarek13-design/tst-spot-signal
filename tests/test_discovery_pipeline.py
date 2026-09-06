import unittest

from research.discovery_pipeline import (
    conditional_forward_stats,
    decluster_events,
    evaluate_candidate,
    feature_redundancy_matrix,
    redundant_pairs,
)


class DiscoveryPipelineTests(unittest.TestCase):
    def test_decluster_events(self):
        self.assertEqual(decluster_events([1, 2, 3, 10, 12], min_gap=3), [1, 10])

    def test_conditional_forward_gate_passes_strong_edge(self):
        closes = [100.0]
        highs = [100.0]
        lows = [100.0]
        for i in range(1, 120):
            base = closes[-1] * (1.02 if i % 4 == 0 else 1.001)
            closes.append(base)
            highs.append(base * 1.02)
            lows.append(base * 0.999)
        idx = list(range(4, 70, 4))
        result = conditional_forward_stats(closes, highs, lows, idx, horizon=4, direction=1)
        self.assertGreater(result.mean_return, 0.015)
        self.assertGreater(result.hit_rate, 0.55)
        self.assertGreaterEqual(result.mfe_mae_ratio, 2.0)

    def test_evaluate_candidate_requires_min_events(self):
        c = [100.0 + i for i in range(100)]
        h = [x * 1.01 for x in c]
        l = [x * 0.99 for x in c]
        ev = evaluate_candidate("tiny", c, h, l, [10, 20], horizons=(5,), min_events=3)
        self.assertFalse(ev.all_required_pass)

    def test_redundancy_flags_duplicate_features(self):
        features = {
            "a": [1, 2, 3, 4, 5],
            "b": [2, 4, 6, 8, 10],
            "c": [5, 1, 4, 2, 3],
        }
        matrix = feature_redundancy_matrix(features)
        self.assertAlmostEqual(matrix[("a", "b")], 1.0, places=12)
        pairs = redundant_pairs(features, threshold=0.95)
        self.assertTrue(any({a, b} == {"a", "b"} for a, b, _ in pairs))


if __name__ == "__main__":
    unittest.main()
