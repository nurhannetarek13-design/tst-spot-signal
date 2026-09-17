import unittest

from v2_bot.reconciliation import ReconciliationItem, ReconciliationReport
from v2_bot.recovery_plan import build_recovery_plan


class RecoveryPlanTests(unittest.TestCase):
    def _report(self, classification: str) -> ReconciliationReport:
        return ReconciliationReport(
            pending_count=1,
            unresolved_count=1,
            can_trade=True,
            items=(
                ReconciliationItem(
                    client_order_id="v2b123",
                    symbol="SOLUSDT",
                    journal_stage="OCO_UNKNOWN",
                    classification=classification,
                    order_status="FILLED",
                    order_list_status=None,
                    evidence={"executed_qty": "0.1"},
                ),
            ),
        )

    def test_clear_report_produces_no_actions(self):
        report = ReconciliationReport(
            pending_count=0,
            unresolved_count=0,
            can_trade=None,
            items=(),
        )
        plan = build_recovery_plan(report)
        self.assertTrue(plan.clear)
        self.assertEqual(plan.items, ())

    def test_existing_protection_is_review_only(self):
        plan = build_recovery_plan(self._report("protection_order_list_found"))
        item = plan.items[0]
        self.assertEqual(item.proposed_action, "review_mark_protected")
        self.assertFalse(item.automation_allowed)

    def test_filled_buy_without_protection_never_suggests_second_buy(self):
        plan = build_recovery_plan(
            self._report("filled_buy_requires_protection_review")
        )
        item = plan.items[0]
        self.assertEqual(item.proposed_action, "review_unprotected_exposure")
        self.assertFalse(item.automation_allowed)
        self.assertNotIn("second BUY", item.proposed_action)

    def test_zero_fill_is_not_auto_aborted(self):
        plan = build_recovery_plan(
            self._report("terminal_zero_fill_candidate_for_abort")
        )
        item = plan.items[0]
        self.assertEqual(item.proposed_action, "review_mark_aborted")
        self.assertFalse(item.automation_allowed)

    def test_unknown_classification_fails_closed_to_manual_review(self):
        plan = build_recovery_plan(self._report("new_unknown_case"))
        item = plan.items[0]
        self.assertEqual(item.proposed_action, "manual_account_reconciliation")
        self.assertFalse(item.automation_allowed)


if __name__ == "__main__":
    unittest.main()
