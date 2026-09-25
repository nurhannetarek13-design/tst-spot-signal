import unittest

from scripts.champion_challenger_gate import evaluate


POLICY={
    "min_oos_trades":100,
    "min_oos_symbols":5,
    "min_profit_factor":1.15,
    "min_net_expectancy_usdt":0.0,
    "max_drawdown_fraction":0.10,
    "require_zero_lookahead_violations":True,
    "require_survivorship_bias_check":True,
    "min_shadow_trades":50,
    "max_shadow_safety_incidents":0,
    "min_shadow_net_expectancy_usdt":0.0,
}


class ChampionChallengerTests(unittest.TestCase):
    def good_replay(self):
        return {
            "trades":120,"symbol_count":8,"profit_factor":1.3,
            "net_expectancy":0.02,"max_drawdown":0.05,
            "lookahead_violations":0,"survivorship_bias_checked":True,
        }

    def good_shadow(self):
        return {"trades":60,"net_expectancy":0.01,"safety_incidents":0}

    def test_good_evidence_only_allows_manual_review(self):
        x=evaluate(POLICY,self.good_replay(),self.good_shadow())
        self.assertTrue(x["promotion_eligible_for_manual_review"])
        self.assertFalse(x["auto_promote"])
        self.assertEqual(x["decision"],"MANUAL_REVIEW_ALLOWED")

    def test_one_safety_incident_blocks_promotion(self):
        shadow=self.good_shadow()
        shadow["safety_incidents"]=1
        x=evaluate(POLICY,self.good_replay(),shadow)
        self.assertFalse(x["promotion_eligible_for_manual_review"])
        self.assertFalse(x["checks"]["shadow_safety_incidents"]["pass"])

    def test_survivorship_or_lookahead_failure_blocks_promotion(self):
        replay=self.good_replay()
        replay["survivorship_bias_checked"]=False
        replay["lookahead_violations"]=1
        x=evaluate(POLICY,replay,self.good_shadow())
        self.assertFalse(x["promotion_eligible_for_manual_review"])


if __name__=="__main__":
    unittest.main()
