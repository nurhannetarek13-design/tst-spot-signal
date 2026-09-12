from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal

import httpx

from v2_bot.binance_private import BinanceAPIError
from v2_bot.execution_journal import ExecutionJournal
from v2_bot.protected_executor import ProtectedSpotExecutor, RecoveryRequired


class FakeSignedClient:
    def __init__(self) -> None:
        self.buy_result = {
            "orderId": 1,
            "status": "FILLED",
            "executedQty": "1.000",
            "cummulativeQuoteQty": "102.00",
            "fills": [],
        }
        self.buy_exc: Exception | None = None
        self.query_order_result = dict(self.buy_result)
        self.query_order_exc: Exception | None = None
        self.oco_result = {
            "orderListId": 7,
            "listOrderStatus": "EXECUTING",
        }
        self.oco_exc: Exception | None = None
        self.query_list_result = {
            "orderListId": 7,
            "listOrderStatus": "EXECUTING",
        }
        self.query_list_exc: Exception | None = None
        self.sell_result = {
            "orderId": 9,
            "status": "FILLED",
            "executedQty": "1.000",
            "cummulativeQuoteQty": "101.00",
        }
        self.sell_exc: Exception | None = None
        self.calls: list[tuple[str, dict]] = []

    def market_buy_quote(self, **kwargs):
        self.calls.append(("buy", kwargs))
        if self.buy_exc:
            raise self.buy_exc
        return self.buy_result

    def query_order(self, **kwargs):
        self.calls.append(("query_order", kwargs))
        if self.query_order_exc:
            raise self.query_order_exc
        return self.query_order_result

    def place_oco_market_protection(self, **kwargs):
        self.calls.append(("oco", kwargs))
        if self.oco_exc:
            raise self.oco_exc
        return self.oco_result

    def query_order_list(self, **kwargs):
        self.calls.append(("query_list", kwargs))
        if self.query_list_exc:
            raise self.query_list_exc
        return self.query_list_result

    def market_sell_quantity(self, **kwargs):
        self.calls.append(("sell", kwargs))
        if self.sell_exc:
            raise self.sell_exc
        return self.sell_result


class ProtectedSpotExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = ExecutionJournal(f"{self.tmp.name}/journal.sqlite3")
        self.client = FakeSignedClient()
        self.executor = ProtectedSpotExecutor(client=self.client, journal=self.journal)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def execute(self, **overrides):
        params = {
            "symbol": "SOLUSDT",
            "base_asset": "SOL",
            "signal_key": "1700000000000",
            "quote_size": Decimal("10"),
            "step_size": Decimal("0.001"),
            "min_qty": Decimal("0.001"),
            "min_notional": Decimal("5"),
            "take_profit_trigger": Decimal("103.20"),
            "stop_loss_trigger": Decimal("101.00"),
        }
        params.update(overrides)
        return self.executor.execute(**params)

    def test_happy_path_is_protected_and_never_flattens(self) -> None:
        result = self.execute()
        self.assertEqual(result.status, "PROTECTED")
        record = self.journal.get(result.client_order_id)
        self.assertEqual(record.stage, "PROTECTED")
        self.assertEqual(record.details["buy_cumulative_quote_qty"], "102.00")
        self.assertEqual([name for name, _ in self.client.calls], ["buy", "oco"])

    def test_base_asset_commission_is_removed_before_oco(self) -> None:
        self.client.buy_result = {
            "orderId": 1,
            "status": "FILLED",
            "executedQty": "1.000",
            "cummulativeQuoteQty": "102.00",
            "fills": [{"commission": "0.001", "commissionAsset": "SOL"}],
        }
        result = self.execute()
        self.assertEqual(result.protected_quantity, Decimal("0.999"))
        oco_call = next(kwargs for name, kwargs in self.client.calls if name == "oco")
        self.assertEqual(oco_call["quantity"], Decimal("0.999"))

    def test_buy_timeout_reconciles_by_same_client_order_id(self) -> None:
        self.client.buy_exc = httpx.ReadTimeout(
            "timeout",
            request=httpx.Request("POST", "https://api.binance.com/api/v3/order"),
        )
        result = self.execute()
        self.assertEqual(result.status, "PROTECTED")
        self.assertEqual(
            [name for name, _ in self.client.calls],
            ["buy", "query_order", "oco"],
        )

    def test_unresolved_buy_timeout_blocks_retry(self) -> None:
        self.client.buy_exc = httpx.ReadTimeout(
            "timeout",
            request=httpx.Request("POST", "https://api.binance.com/api/v3/order"),
        )
        self.client.query_order_exc = BinanceAPIError(
            status_code=400,
            code=-2013,
            message="Order does not exist",
        )
        with self.assertRaises(RecoveryRequired):
            self.execute()
        pending = self.journal.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].stage, "BUY_UNKNOWN")
        self.assertNotIn("oco", [name for name, _ in self.client.calls])
        self.assertNotIn("sell", [name for name, _ in self.client.calls])

    def test_definitive_oco_rejection_emergency_flattens(self) -> None:
        self.client.oco_exc = BinanceAPIError(
            status_code=400,
            code=-1013,
            message="Filter failure",
        )
        result = self.execute()
        self.assertEqual(result.status, "FLATTENED")
        self.assertEqual(self.journal.get(result.client_order_id).stage, "FLATTENED")
        self.assertIn("sell", [name for name, _ in self.client.calls])

    def test_emergency_sell_must_be_confirmed_filled(self) -> None:
        self.client.oco_exc = BinanceAPIError(
            status_code=400,
            code=-1013,
            message="Filter failure",
        )
        self.client.sell_result = {
            "orderId": 9,
            "status": "EXPIRED",
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
        }
        with self.assertRaises(RecoveryRequired):
            self.execute()
        pending = self.journal.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].stage, "UNPROTECTED")
        self.assertEqual(pending[0].details["flatten_status"], "EXPIRED")

    def test_oco_timeout_reconciles_and_does_not_flatten_when_found(self) -> None:
        self.client.oco_exc = httpx.ReadTimeout(
            "timeout",
            request=httpx.Request("POST", "https://api.binance.com/api/v3/orderList/oco"),
        )
        result = self.execute()
        self.assertEqual(result.status, "PROTECTED")
        self.assertEqual(
            [name for name, _ in self.client.calls],
            ["buy", "oco", "query_list"],
        )
        self.assertNotIn("sell", [name for name, _ in self.client.calls])

    def test_unknown_oco_does_not_blindly_flatten_or_replace_it(self) -> None:
        self.client.oco_exc = httpx.ReadTimeout(
            "timeout",
            request=httpx.Request("POST", "https://api.binance.com/api/v3/orderList/oco"),
        )
        self.client.query_list_exc = BinanceAPIError(
            status_code=400,
            code=-2013,
            message="Order list does not exist",
        )
        with self.assertRaises(RecoveryRequired):
            self.execute()
        pending = self.journal.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].stage, "OCO_UNKNOWN")
        self.assertNotIn("sell", [name for name, _ in self.client.calls])

    def test_post_fill_quantity_filter_failure_flattens_before_oco(self) -> None:
        self.client.buy_result = {
            "orderId": 1,
            "status": "FILLED",
            "executedQty": "0.050",
            "cummulativeQuoteQty": "5.10",
            "fills": [],
        }
        self.client.sell_result["executedQty"] = "0.050"
        result = self.execute(min_notional=Decimal("6"))
        self.assertEqual(result.status, "FLATTENED")
        self.assertNotIn("oco", [name for name, _ in self.client.calls])
        self.assertIn("sell", [name for name, _ in self.client.calls])

    def test_existing_pending_execution_blocks_fresh_buy(self) -> None:
        self.journal.begin(
            client_order_id="old-pending",
            symbol="ETHUSDT",
            details={"test": True},
        )
        with self.assertRaises(RecoveryRequired):
            self.execute()
        self.assertEqual(self.client.calls, [])

    def test_active_protected_exposure_blocks_second_buy(self) -> None:
        first = self.execute()
        self.assertEqual(first.status, "PROTECTED")
        self.client.calls.clear()
        with self.assertRaisesRegex(RecoveryRequired, "LIVE_POSITION_LIMIT"):
            self.execute(signal_key="different-signal")
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
