from __future__ import annotations

import unittest

from research.tardis_microstructure_features import (
    extract_microstructure_rows,
    frozen_gate_g_diagnostic,
    sample_last_per_second,
)


def row(line, data, prefix="2021-09-01T00:00:00.000Z"):
    return {"line": line, "prefix": prefix, "stream": None, "data": data}


def base_rows():
    return [
        row(0, {"lastUpdateId": 100, "bids": [["100", "5"], ["99", "3"]], "asks": [["101", "4"], ["102", "2"]]}),
        row(1, {"E": 1000, "U": 101, "u": 101, "pu": 100, "b": [["100", "6"]], "a": []}),
        row(2, {"E": 1500, "U": 102, "u": 102, "pu": 101, "b": [["99", "0"]], "a": [["102", "3"]]}),
        row(3, {"E": 1900, "u": 102, "b": "100", "B": "6", "a": "101", "A": "4"}),
        row(4, {"E": 2200, "U": 103, "u": 103, "pu": 102, "b": [["100", "0"], ["99.5", "2"]], "a": []}),
        row(5, {"E": 2300, "u": 103, "b": "99.5", "B": "2", "a": "101", "A": "4"}),
    ]


class FeatureExtractionTests(unittest.TestCase):
    def test_features_math_and_deletion(self):
        result = extract_microstructure_rows(base_rows(), symbol="BTCUSDT")
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["validation"]["canonicalReplayReady"])
        feats = result["features"]
        self.assertEqual(len(feats), 3)
        first = feats[0]
        expected_micro = (101 * 6 + 100 * 4) / 10
        self.assertAlmostEqual(first["microprice"], expected_micro)
        self.assertAlmostEqual(first["depth_imbalance_1"], (6 - 4) / (6 + 4))
        self.assertIsNone(first["ofi_depth_proxy_10"])
        self.assertIsNotNone(feats[1]["ofi_depth_proxy_10"])
        self.assertEqual(feats[2]["best_bid"], 99.5)

    def test_bad_bridge_is_rejected_before_features(self):
        rows = base_rows()
        rows[1]["data"]["pu"] = 99
        result = extract_microstructure_rows(rows, symbol="BTCUSDT")
        self.assertEqual(result["status"], "REPLAY_NOT_READY")
        self.assertEqual(result["validation"]["status"], "SNAPSHOT_PU_BRIDGE_FAIL")
        self.assertEqual(result["features"], [])

    def test_pu_gap_is_rejected(self):
        rows = base_rows()
        rows[2]["data"]["pu"] = 999
        result = extract_microstructure_rows(rows, symbol="BTCUSDT")
        self.assertEqual(result["status"], "REPLAY_NOT_READY")
        self.assertEqual(result["validation"]["status"], "PU_GAP")

    def test_last_observation_per_second(self):
        result = extract_microstructure_rows(base_rows(), symbol="BTCUSDT")
        samples = sample_last_per_second(result["features"])
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0]["update_id"], 102)
        self.assertEqual(samples[1]["update_id"], 103)

    def test_gate_g_frozen_event_and_forward_outcome(self):
        samples = []
        for i in range(75):
            samples.append({
                "timestamp_ms": i * 1000,
                "mid": 100.0 + i * 0.01,
                "depth_imbalance_10": 0.7 if i == 0 else 0.0,
                "microprice_deviation_bps": 0.1 if i == 0 else 0.0,
            })
        result = frozen_gate_g_diagnostic(samples)
        self.assertEqual(result["eventCounts"]["long"], 1)
        self.assertEqual(result["eventCounts"]["short"], 0)
        self.assertEqual(result["outcomes"]["long"]["10"]["n"], 1)
        self.assertGreater(result["outcomes"]["long"]["10"]["meanDirectionalBps"], 0)


if __name__ == "__main__":
    unittest.main()
