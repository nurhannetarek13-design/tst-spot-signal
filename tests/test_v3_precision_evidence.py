import unittest

from scripts.build_v3_precision_evidence import build_evidence


class V3PrecisionEvidenceTests(unittest.TestCase):
    def good_inputs(self):
        holdout = {
            "trades": 100,
            "wins": 99,
            "losses": 1,
            "winRate": 0.99,
            "netPnlPerUnit": 1.0,
            "expectancyPerUnit": 0.01,
            "profitFactor": 2.0,
            "untouched": True,
            "costsIncluded": True,
        }
        backtest = {"productionGradeHistoricalEvidence": True}
        universe = {
            "pointInTimeUniverse": True,
            "delistedCoverage": True,
            "historicalSymbolCount": 150,
            "delistedSymbolCount": 20,
        }
        replay = {"replayIntegrityPass": True, "executionReplayPass": True}
        return holdout, backtest, universe, replay

    def test_missing_evidence_fails_closed(self):
        x = build_evidence()
        self.assertFalse(x["claimSupported"])
        self.assertIn("HOLDOUT_SAMPLE_TOO_SMALL", x["claimBlockedReasons"])
        self.assertIn("POINT_IN_TIME_UNIVERSE_NOT_PROVEN", x["claimBlockedReasons"])
        self.assertFalse(x["liveTrading"])

    def test_exact_99_percent_can_pass_only_with_all_integrity_evidence(self):
        h, b, u, r = self.good_inputs()
        x = build_evidence(h, b, u, r)
        self.assertTrue(x["claimSupported"])
        self.assertEqual(x["holdout"]["winRate"], 0.99)
        self.assertEqual(x["holdout"]["trades"], 100)
        self.assertFalse(x["liveTrading"])

    def test_peeked_holdout_is_rejected_even_at_100_percent(self):
        h, b, u, r = self.good_inputs()
        h.update({"wins": 100, "losses": 0, "winRate": 1.0, "untouched": False})
        x = build_evidence(h, b, u, r)
        self.assertFalse(x["claimSupported"])
        self.assertIn("HOLDOUT_NOT_EXPLICITLY_UNTOUCHED", x["claimBlockedReasons"])

    def test_current_universe_only_is_rejected(self):
        h, b, u, r = self.good_inputs()
        u["pointInTimeUniverse"] = False
        u["delistedCoverage"] = False
        x = build_evidence(h, b, u, r)
        self.assertFalse(x["claimSupported"])
        self.assertIn("DELISTED_COVERAGE_NOT_PROVEN", x["claimBlockedReasons"])

    def test_execution_replay_is_mandatory(self):
        h, b, u, r = self.good_inputs()
        r["executionReplayPass"] = False
        x = build_evidence(h, b, u, r)
        self.assertFalse(x["claimSupported"])
        self.assertIn("SPOT_EXECUTION_REPLAY_NOT_PROVEN", x["claimBlockedReasons"])


if __name__ == "__main__":
    unittest.main()
