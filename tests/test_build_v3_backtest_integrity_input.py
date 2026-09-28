import unittest

from scripts.build_v3_backtest_integrity_input import build


class BuildV3BacktestIntegrityInputTests(unittest.TestCase):
    def test_machine_evidence_upgrades_only_supported_fields(self):
        base={
            "chronologicalOOS":True,
            "walkForward":True,
            "unseenSymbolHoldout":True,
            "lookAheadAudit":True,
            "feesIncluded":True,
            "spreadIncluded":True,
            "slippageIncluded":True,
            "latencyIncluded":False,
            "partialFillsIncluded":False,
            "roundTripCostBps":28,
        }
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
            "latencyIncluded":True,
            "partialFillAccountingIncluded":True,
        }
        x=build(base,universe,replay)
        self.assertTrue(x["pointInTimeUniverse"])
        self.assertTrue(x["delistedCoverage"])
        self.assertTrue(x["latencyIncluded"])
        self.assertTrue(x["partialFillsIncluded"])
        self.assertEqual(x["historicalSymbolCount"],660)
        self.assertEqual(x["delistedSymbolCount"],192)
        self.assertTrue(x["chronologicalOOS"])

    def test_missing_replay_evidence_fails_closed(self):
        x=build({"latencyIncluded":True,"partialFillsIncluded":True},
                {"pointInTimeUniverse":True,"delistedCoverage":True},
                {"replayIntegrityPass":True,"executionReplayPass":True})
        self.assertFalse(x["latencyIncluded"])
        self.assertFalse(x["partialFillsIncluded"])

    def test_missing_universe_evidence_fails_closed(self):
        x=build({}, {}, {
            "replayIntegrityPass":True,
            "executionReplayPass":True,
            "latencyIncluded":True,
            "partialFillAccountingIncluded":True,
        })
        self.assertFalse(x["pointInTimeUniverse"])
        self.assertFalse(x["delistedCoverage"])
        self.assertEqual(x["historicalSymbolCount"],0)
        self.assertEqual(x["delistedSymbolCount"],0)


if __name__=="__main__":
    unittest.main()
