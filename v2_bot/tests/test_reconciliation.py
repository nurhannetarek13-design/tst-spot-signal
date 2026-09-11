import tempfile
import unittest
from pathlib import Path

from v2_bot.execution_journal import ExecutionJournal
from v2_bot.reconciliation import ReadOnlyExecutionReconciler


class FakeClient:
    def __init__(self):
        self.account = {"canTrade": True, "balances": []}
        self.orders = {}
        self.order_lists = {}
        self.calls = []

    def account_information(self, *, omit_zero_balances=True):
        self.calls.append(("account", omit_zero_balances))
        return self.account

    def query_order(self, *, symbol, client_order_id):
        self.calls.append(("order", symbol, client_order_id))
        result = self.orders.get(client_order_id)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise RuntimeError("missing order")
        return result

    def query_order_list(self, *, list_client_order_id):
        self.calls.append(("order_list", list_client_order_id))
        result = self.order_lists.get(list_client_order_id)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise RuntimeError("missing order list")
        return result


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "state.sqlite3")
        self.journal = ExecutionJournal(self.db)
        self.client = FakeClient()
        self.reconciler = ReadOnlyExecutionReconciler(
            client=self.client,
            journal=self.journal,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_clear_journal_requires_no_private_queries(self):
        report = self.reconciler.inspect()
        self.assertTrue(report.clear)
        self.assertEqual(report.pending_count, 0)
        self.assertEqual(self.client.calls, [])

    def test_buy_unknown_found_filled_is_reported_but_not_mutated(self):
        record = self.journal.begin(
            client_order_id="buy1",
            symbol="SOLUSDT",
            details={"oco_list_client_order_id": "oco1"},
        )
        self.journal.transition(record.client_order_id, "BUY_UNKNOWN")
        self.client.orders["buy1"] = {
            "orderId": 11,
            "status": "FILLED",
            "executedQty": "0.1",
        }

        report = self.reconciler.inspect()

        self.assertFalse(report.clear)
        self.assertEqual(report.unresolved_count, 1)
        self.assertEqual(report.items[0].classification, "filled_buy_requires_protection_review")
        self.assertEqual(self.journal.get("buy1").stage, "BUY_UNKNOWN")

    def test_oco_unknown_with_existing_order_list_is_identified(self):
        record = self.journal.begin(
            client_order_id="buy2",
            symbol="BTCUSDT",
            details={"oco_list_client_order_id": "oco2"},
        )
        self.journal.transition(record.client_order_id, "BUY_FILLED")
        self.journal.transition(record.client_order_id, "OCO_INTENT")
        self.journal.transition(record.client_order_id, "OCO_UNKNOWN")
        self.client.orders["buy2"] = {
            "orderId": 12,
            "status": "FILLED",
            "executedQty": "0.001",
        }
        self.client.order_lists["oco2"] = {
            "orderListId": 77,
            "listOrderStatus": "EXECUTING",
        }

        report = self.reconciler.inspect()

        self.assertEqual(report.items[0].classification, "protection_order_list_found")
        self.assertEqual(report.items[0].order_list_status, "EXECUTING")
        self.assertEqual(self.journal.get("buy2").stage, "OCO_UNKNOWN")

    def test_terminal_zero_fill_is_only_candidate_for_abort_not_auto_mutated(self):
        record = self.journal.begin(
            client_order_id="buy3",
            symbol="ETHUSDT",
        )
        self.journal.transition(record.client_order_id, "BUY_UNKNOWN")
        self.client.orders["buy3"] = {
            "orderId": 13,
            "status": "EXPIRED",
            "executedQty": "0",
        }

        report = self.reconciler.inspect()

        self.assertEqual(
            report.items[0].classification,
            "terminal_zero_fill_candidate_for_abort",
        )
        self.assertEqual(self.journal.get("buy3").stage, "BUY_UNKNOWN")

    def test_unconfirmed_order_stays_manual_recovery_case(self):
        self.journal.begin(client_order_id="buy4", symbol="XRPUSDT")
        report = self.reconciler.inspect()
        self.assertEqual(report.items[0].classification, "buy_state_not_confirmed")
        self.assertEqual(self.journal.get("buy4").stage, "INTENT")


if __name__ == "__main__":
    unittest.main()
