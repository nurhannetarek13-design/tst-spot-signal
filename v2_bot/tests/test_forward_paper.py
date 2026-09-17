import unittest

from v2_bot.forward_paper import (
    FORWARD_PAPER_STATUS,
    FORWARD_PAPER_STRATEGY_ID,
    registry_snapshot_for_mode,
    runtime_specs,
)
from v2_bot.strategy_pool import ACTIVE, PAPER, RESEARCH


class ForwardPaperTests(unittest.TestCase):
    def test_shadow_keeps_all_research_statuses(self):
        specs = runtime_specs("shadow")
        target = next(x for x in specs if x.strategy_id == FORWARD_PAPER_STRATEGY_ID)
        self.assertEqual(target.status, RESEARCH)

    def test_paper_enables_only_one_forward_experiment(self):
        specs = runtime_specs("paper")
        enabled = [x.strategy_id for x in specs if x.status == PAPER]
        self.assertEqual(enabled, [FORWARD_PAPER_STRATEGY_ID])
        self.assertFalse(any(x.status == ACTIVE for x in specs))

    def test_live_never_promotes_forward_experiment(self):
        specs = runtime_specs("live")
        target = next(x for x in specs if x.strategy_id == FORWARD_PAPER_STRATEGY_ID)
        self.assertEqual(target.status, RESEARCH)

    def test_registry_labels_forward_paper_without_calling_it_promoted(self):
        rows = registry_snapshot_for_mode("paper")
        target = next(x for x in rows if x["strategy_id"] == FORWARD_PAPER_STRATEGY_ID)
        self.assertEqual(target["status"], FORWARD_PAPER_STATUS)


if __name__ == "__main__":
    unittest.main()
