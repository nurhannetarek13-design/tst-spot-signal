from __future__ import annotations

import tempfile
import unittest

from v2_bot.execution_journal import ExecutionJournal
from v2_bot.live_exposure import reconcile_active_protected


class FakeClient:
    def __init__(self):
        self.list_result = {
            "orderListId": 77,
            "listOrderStatus": "EXECUTING",
            "orders": [
                {"clientOrderId": "tp-child"},
                {"clientOrderId": "sl-child"},
            ],
        }
        self.orders = {
            "tp-child": {
                "clientOrderId": "tp-child",
                "orderId": 101,
                "side": "SELL",
                "status": "NEW",
                "executedQty": "0",
                "cummulativeQuoteQty": "0",
            },
            "sl-child": {
                "clientOrderId": "sl-child",
                "orderId": 102,
                "side": "SELL",
                "status": "NEW",
                "executedQty": "0",
                "cummulativeQuoteQty": "0",
            },
        }

    def query_order_list(self, **_kwargs):
        return self.list_result

    def query_order(self, *, client_order_id, **_kwargs):
        return self.orders[client_order_id]


class LiveExposureReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = ExecutionJournal(f"{self.tmp.name}/journal.sqlite3")
        self.client = FakeClient()

    def tearDown(self):
        self.tmp.cleanup()

    def protected(self, *, include_list_id=True):
        details = {"buy_cumulative_quote_qty": "10.00"}
        if include_list_id:
            details["oco_list_client_order_id"] = "v2olist"
        record = self.journal.begin(
            client_order_id="v2buy",
            symbol="SOLUSDT",
            details=details,
        )
        self.journal.transition(record.client_order_id, "BUY_FILLED")
        self.journal.transition(record.client_order_id, "OCO_INTENT")
        return self.journal.transition(record.client_order_id, "PROTECTED")

    def test_executing_oco_keeps_active_exposure(self):
        self.protected()
        events = reconcile_active_protected(client=self.client, journal=self.journal)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, "PROTECTED")
        self.assertTrue(self.journal.has_active_protected())
        self.assertFalse(self.journal.has_pending())

    def test_verified_filled_sell_closes_exposure(self):
        self.protected()
        self.client.list_result["listOrderStatus"] = "ALL_DONE"
        self.client.orders["tp-child"] = {
            "clientOrderId": "tp-child",
            "orderId": 101,
            "side": "SELL",
            "status": "FILLED",
            "executedQty": "0.097",
            "cummulativeQuoteQty": "10.09",
        }
        self.client.orders["sl-child"]["status"] = "CANCELED"

        events = reconcile_active_protected(client=self.client, journal=self.journal)
        self.assertEqual(events[0].status, "CLOSED")
        record = self.journal.get("v2buy")
        self.assertEqual(record.stage, "CLOSED")
        self.assertEqual(record.details["exit_cumulative_quote_qty"], "10.09")
        self.assertFalse(self.journal.has_active_protected())
        self.assertFalse(self.journal.has_pending())

    def test_all_done_without_verified_sell_fails_closed(self):
        self.protected()
        self.client.list_result["listOrderStatus"] = "ALL_DONE"
        self.client.orders["tp-child"]["status"] = "CANCELED"
        self.client.orders["sl-child"]["status"] = "CANCELED"

        events = reconcile_active_protected(client=self.client, journal=self.journal)
        self.assertEqual(events[0].status, "UNPROTECTED")
        record = self.journal.get("v2buy")
        self.assertEqual(record.stage, "UNPROTECTED")
        self.assertTrue(self.journal.has_pending())
        self.assertFalse(self.journal.has_active_protected())

    def test_missing_oco_client_id_fails_closed(self):
        self.protected(include_list_id=False)
        events = reconcile_active_protected(client=self.client, journal=self.journal)
        self.assertEqual(events[0].status, "UNPROTECTED")
        self.assertEqual(self.journal.get("v2buy").stage, "UNPROTECTED")
        self.assertTrue(self.journal.has_pending())


if __name__ == "__main__":
    unittest.main()
