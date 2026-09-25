import unittest

from scripts.evaluate_v3_champion_challenger import evaluate


def state(pnls, slip=4):
    return {
        "closed_trades":[{"pnl_usdt":x,"mfe_r":1.0,"mae_r":0.3} for x in pnls],
        "slippage_model":{"TESTUSDT":{"count":len(pnls) or 1,"ewma_bps":slip}},
    }


REG={
    "promotionPolicy":{
        "minimumClosedTrades":100,
        "minimumProfitFactor":1.2,
        "expectancyMustBePositive":True,
        "maximumDrawdownUSDT":2.0,
        "maximumMedianEntrySlippageBps":12,
        "requireSpotMicrostructureReplay":True,
        "requireNoCriticalDataIncidents":True,
        "requireNoLiveOrdersDuringEvaluation":True,
        "challengerMustBeatChampionProfitFactorByFraction":0.05,
        "challengerMustNotIncreaseDrawdownByFraction":0.10,
        "automaticPromotion":False,
    }
}


class ChampionChallengerTests(unittest.TestCase):
    def test_small_sample_cannot_promote(self):
        r=evaluate(state([.1]*20),state([.2]*20),REG,{"replayIntegrityPass":True,"executionReplayPass":True})
        self.assertFalse(r["evidencePass"])
        self.assertFalse(r["automaticPromotion"])
        self.assertIn("CHALLENGER_SAMPLE_TOO_SMALL",r["reasons"])

    def test_missing_spot_replay_blocks(self):
        champion=state([.1]*70+[-.05]*30,3)
        challenger=state([.15]*75+[-.04]*25,4)
        r=evaluate(champion,challenger,REG,None)
        self.assertFalse(r["evidencePass"])
        self.assertIn("SPOT_MICROSTRUCTURE_REPLAY_NOT_PROVEN",r["reasons"])

    def test_even_passing_evidence_never_auto_promotes(self):
        champion=state([.1]*70+[-.05]*30,3)
        challenger=state([.2]*80+[-.04]*20,4)
        r=evaluate(champion,challenger,REG,{"replayIntegrityPass":True,"executionReplayPass":True})
        self.assertTrue(r["evidencePass"])
        self.assertTrue(r["promotionReadyForManualReview"])
        self.assertFalse(r["automaticPromotion"])
        self.assertFalse(r["liveTrading"])


if __name__=="__main__":
    unittest.main()
