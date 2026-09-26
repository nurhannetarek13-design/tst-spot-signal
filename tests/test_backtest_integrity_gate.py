import unittest
from research.backtest_integrity_gate import evaluate

class BacktestIntegrityGateTests(unittest.TestCase):
    def test_current_universe_only_fails_survivorship_gate(self):
        x=evaluate({
            "pointInTimeUniverse":False,"delistedCoverage":False,
            "chronologicalOOS":True,"walkForward":True,"unseenSymbolHoldout":True,
            "lookAheadAudit":True,"feesIncluded":True,"spreadIncluded":True,
            "slippageIncluded":True,"latencyIncluded":True,"partialFillsIncluded":True,
            "historicalSymbolCount":24,"delistedSymbolCount":0,"roundTripCostBps":28,
        })
        self.assertFalse(x["productionGradeHistoricalEvidence"])
        self.assertIn("pointInTimeUniverse:MISSING_OR_FALSE",x["reasons"])
        self.assertIn("NO_DELISTED_SYMBOLS",x["reasons"])

    def test_complete_point_in_time_evidence_passes(self):
        x=evaluate({
            "pointInTimeUniverse":True,"delistedCoverage":True,
            "chronologicalOOS":True,"walkForward":True,"unseenSymbolHoldout":True,
            "lookAheadAudit":True,"feesIncluded":True,"spreadIncluded":True,
            "slippageIncluded":True,"latencyIncluded":True,"partialFillsIncluded":True,
            "historicalSymbolCount":150,"delistedSymbolCount":25,"roundTripCostBps":28,
        })
        self.assertTrue(x["productionGradeHistoricalEvidence"])
        self.assertEqual(x["reasons"],[])

if __name__=="__main__":
    unittest.main()
