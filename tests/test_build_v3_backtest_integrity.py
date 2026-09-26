import unittest

from scripts.build_v3_backtest_integrity import build


BASE={
    "chronologicalOOS":True,
    "walkForward":True,
    "unseenSymbolHoldout":True,
    "lookAheadAudit":True,
    "feesIncluded":True,
    "spreadIncluded":True,
    "slippageIncluded":True,
    "roundTripCostBps":28,
}


class BuildV3BacktestIntegrityTests(unittest.TestCase):
    def test_current_machine_evidence_can_complete_dynamic_integrity_fields(self):
        universe={
            "engine":"BINANCE_VISION_POINT_IN_TIME_SPOT_UNIVERSE_V1",
            "pointInTimeUniverse":True,
            "delistedCoverage":True,
            "historicalSymbolCount":660,
            "delistedSymbolCount":192,
        }
        replay={
            "engine":"SPOT_V3_MICROSTRUCTURE_REPLAY_AGGREGATE",
            "replayIntegrityPass":True,
            "executionReplayPass":True,
            "executionProbeCount":12,
            "filledExecutionProbeCount":12,
            "latencyIncluded":True,
            "partialFillsIncluded":True,
            "spreadIncluded":True,
            "slippageIncluded":True,
            "roundTripCostBps":28,
        }
        x=build(BASE,universe,replay)
        self.assertTrue(x["productionGradeHistoricalEvidence"])
        self.assertEqual(x["historicalSymbolCount"],660)
        self.assertEqual(x["delistedSymbolCount"],192)
        self.assertTrue(x["checks"]["latencyIncluded"])
        self.assertTrue(x["checks"]["partialFillsIncluded"])

    def test_missing_replay_fields_fail_closed(self):
        universe={
            "pointInTimeUniverse":True,"delistedCoverage":True,
            "historicalSymbolCount":660,"delistedSymbolCount":192,
        }
        replay={
            "replayIntegrityPass":True,
            "executionReplayPass":True,
            "spreadIncluded":True,
            "slippageIncluded":True,
            "roundTripCostBps":28,
        }
        x=build(BASE,universe,replay)
        self.assertFalse(x["productionGradeHistoricalEvidence"])
        self.assertIn("latencyIncluded:MISSING_OR_FALSE",x["reasons"])
        self.assertIn("partialFillsIncluded:MISSING_OR_FALSE",x["reasons"])

    def test_missing_universe_fails_closed(self):
        replay={
            "replayIntegrityPass":True,"executionReplayPass":True,
            "latencyIncluded":True,"partialFillsIncluded":True,
            "spreadIncluded":True,"slippageIncluded":True,"roundTripCostBps":28,
        }
        x=build(BASE,{},replay)
        self.assertFalse(x["productionGradeHistoricalEvidence"])
        self.assertIn("pointInTimeUniverse:MISSING_OR_FALSE",x["reasons"])
        self.assertIn("NO_DELISTED_SYMBOLS",x["reasons"])


if __name__=="__main__":
    unittest.main()
