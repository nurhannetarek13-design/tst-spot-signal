import json
import pathlib
import subprocess
import tempfile
import unittest

from scripts.build_v3_forward_holdout import build


class ForwardHoldoutTests(unittest.TestCase):
    def _blob(self,path):
        return subprocess.check_output(["git","hash-object",str(path)],text=True).strip()

    def test_only_post_freeze_trades_count_and_costs_are_required(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=pathlib.Path(td)/"config.json"
            cfg.write_text('{"x":1}\n')
            policy={
                "freezeAt":"2026-09-26T11:50:00Z",
                "expectedEngine":"INDICATOR_ONLY_V3_EARLY_MOMENTUM",
                "configGitBlobSha":self._blob(cfg),
                "frozen":True,
                "requireExecutionCostFields":True,
                "minimumTradesForClaim":100,
                "minimumWinRateForClaim":0.99,
                "requirePositiveNet":True,
            }
            state={
                "mode":"PAPER_ONLY",
                "engine":"INDICATOR_ONLY_V3_EARLY_MOMENTUM",
                "closed_trades":[
                    {
                        "engine":"INDICATOR_ONLY_V3_EARLY_MOMENTUM",
                        "symbol":"OLDUSDT","opened_at":"2026-09-26T10:00:00Z",
                        "pnl_usdt":5,
                    },
                    {
                        "engine":"INDICATOR_ONLY_V3_EARLY_MOMENTUM",
                        "symbol":"NEWUSDT","opened_at":"2026-09-26T12:00:00Z",
                        "pnl_usdt":0.2,
                        "entry_context":{"estimated_entry_slippage_bps":3.0},
                        "exit_slippage_bps":4.0,
                    },
                ],
            }
            x=build(state,policy,cfg)
            self.assertEqual(x["trades"],1)
            self.assertEqual(x["wins"],1)
            self.assertTrue(x["untouched"])
            self.assertTrue(x["costsIncluded"])
            self.assertIn("FORWARD_SAMPLE_TOO_SMALL",x["claimBlockedReasons"])

    def test_config_change_invalidates_untouched_holdout(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=pathlib.Path(td)/"config.json"
            cfg.write_text('{"x":1}\n')
            original=self._blob(cfg)
            cfg.write_text('{"x":2}\n')
            policy={
                "freezeAt":"2026-09-26T11:50:00Z",
                "expectedEngine":"INDICATOR_ONLY_V3_EARLY_MOMENTUM",
                "configGitBlobSha":original,
                "frozen":True,
                "requireExecutionCostFields":True,
                "minimumTradesForClaim":100,
                "minimumWinRateForClaim":0.99,
                "requirePositiveNet":True,
            }
            state={"mode":"PAPER_ONLY","engine":"INDICATOR_ONLY_V3_EARLY_MOMENTUM","closed_trades":[]}
            x=build(state,policy,cfg)
            self.assertFalse(x["configUnchanged"])
            self.assertFalse(x["untouched"])
            self.assertIn("FORWARD_HOLDOUT_NOT_UNTOUCHED",x["claimBlockedReasons"])

    def test_missing_execution_cost_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=pathlib.Path(td)/"config.json"
            cfg.write_text('{"x":1}\n')
            policy={
                "freezeAt":"2026-09-26T11:50:00Z",
                "expectedEngine":"INDICATOR_ONLY_V3_EARLY_MOMENTUM",
                "configGitBlobSha":self._blob(cfg),
                "frozen":True,
                "requireExecutionCostFields":True,
                "minimumTradesForClaim":1,
                "minimumWinRateForClaim":0.99,
                "requirePositiveNet":True,
            }
            state={
                "mode":"PAPER_ONLY","engine":"INDICATOR_ONLY_V3_EARLY_MOMENTUM",
                "closed_trades":[{
                    "engine":"INDICATOR_ONLY_V3_EARLY_MOMENTUM",
                    "symbol":"XUSDT","opened_at":"2026-09-26T12:00:00Z","pnl_usdt":1.0,
                    "entry_context":{},
                }]
            }
            x=build(state,policy,cfg)
            self.assertFalse(x["costsIncluded"])
            self.assertIn("FORWARD_COST_FIELDS_INCOMPLETE",x["claimBlockedReasons"])


if __name__=="__main__":
    unittest.main()
