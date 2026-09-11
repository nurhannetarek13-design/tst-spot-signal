from __future__ import annotations

import hashlib
import hmac
import unittest
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from v2_bot.binance_private import BinanceAPIError, BinanceSignedSpotClient


class FakeHTTPClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)

    def close(self) -> None:
        pass


def response(status: int, payload) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", "https://api.binance.com/test"),
    )


class BinanceSignedSpotClientTests(unittest.TestCase):
    def test_signs_exact_percent_encoded_payload_before_sending(self) -> None:
        fake = FakeHTTPClient(
            [response(200, {"status": "FILLED", "executedQty": "0.1"})]
        )
        client = BinanceSignedSpotClient(
            api_key="key",
            api_secret="secret",
            client=fake,
            clock=lambda: 1700000000.123,
        )

        client.market_buy_quote(
            symbol="BTCUSDT",
            quote_order_qty=Decimal("10"),
            client_order_id="abc/+",
        )

        expected_params = {
            "newClientOrderId": "abc/+",
            "newOrderRespType": "FULL",
            "quoteOrderQty": "10",
            "recvWindow": "5000",
            "side": "BUY",
            "symbol": "BTCUSDT",
            "timestamp": "1700000000123",
            "type": "MARKET",
        }
        encoded = urlencode(sorted(expected_params.items()), safe="")
        signature = hmac.new(
            b"secret",
            encoded.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        expected = f"{encoded}&signature={signature}"

        self.assertEqual(fake.calls[0]["method"], "POST")
        self.assertEqual(fake.calls[0]["content"], expected)
        self.assertIn("newClientOrderId=abc%2F%2B", expected)
        self.assertEqual(fake.calls[0]["headers"]["X-MBX-APIKEY"], "key")

    def test_account_reconciliation_calls_are_get_only(self) -> None:
        fake = FakeHTTPClient(
            [
                response(200, {"canTrade": True, "balances": []}),
                response(200, [{"symbol": "BTCUSDT", "clientOrderId": "abc"}]),
                response(200, [{"symbol": "BTCUSDT", "listClientOrderId": "list1"}]),
            ]
        )
        client = BinanceSignedSpotClient(
            api_key="key",
            api_secret="secret",
            client=fake,
            clock=lambda: 1700000000,
        )

        account = client.account_information(omit_zero_balances=True)
        orders = client.open_orders(symbol="BTCUSDT")
        order_lists = client.open_order_lists()

        self.assertTrue(account["canTrade"])
        self.assertEqual(orders[0]["clientOrderId"], "abc")
        self.assertEqual(order_lists[0]["listClientOrderId"], "list1")
        self.assertEqual([call["method"] for call in fake.calls], ["GET", "GET", "GET"])
        self.assertIn("/api/v3/account?", fake.calls[0]["url"])
        self.assertIn("omitZeroBalances=true", fake.calls[0]["url"])
        self.assertIn("/api/v3/openOrders?", fake.calls[1]["url"])
        self.assertIn("symbol=BTCUSDT", fake.calls[1]["url"])
        self.assertIn("/api/v3/openOrderList?", fake.calls[2]["url"])

    def test_open_orders_omits_symbol_when_not_requested(self) -> None:
        fake = FakeHTTPClient([response(200, [])])
        client = BinanceSignedSpotClient(
            api_key="key",
            api_secret="secret",
            client=fake,
            clock=lambda: 1700000000,
        )
        self.assertEqual(client.open_orders(), [])
        self.assertNotIn("symbol=", fake.calls[0]["url"])

    def test_oco_uses_market_triggered_take_profit_and_stop_loss(self) -> None:
        fake = FakeHTTPClient(
            [response(200, {"orderListId": 12, "listOrderStatus": "EXECUTING"})]
        )
        client = BinanceSignedSpotClient(
            api_key="key",
            api_secret="secret",
            client=fake,
            clock=lambda: 1700000000,
        )

        client.place_oco_market_protection(
            symbol="SOLUSDT",
            quantity=Decimal("0.097"),
            take_profit_trigger=Decimal("103.20"),
            stop_loss_trigger=Decimal("101.00"),
            list_client_order_id="list1",
            above_client_order_id="tp1",
            below_client_order_id="sl1",
        )

        body = fake.calls[0]["content"]
        self.assertIn("aboveType=TAKE_PROFIT", body)
        self.assertIn("aboveStopPrice=103.20", body)
        self.assertIn("belowType=STOP_LOSS", body)
        self.assertIn("belowStopPrice=101.00", body)
        self.assertNotIn("abovePrice=", body)
        self.assertNotIn("belowPrice=", body)

    def test_5xx_is_classified_as_execution_unknown(self) -> None:
        fake = FakeHTTPClient([response(500, {"code": -1000, "msg": "Internal error"})])
        client = BinanceSignedSpotClient(
            api_key="key",
            api_secret="secret",
            client=fake,
            clock=lambda: 1700000000,
        )
        with self.assertRaises(BinanceAPIError) as ctx:
            client.market_buy_quote(
                symbol="BTCUSDT",
                quote_order_qty=Decimal("10"),
                client_order_id="v2-test",
            )
        self.assertTrue(ctx.exception.execution_unknown)
        self.assertEqual(ctx.exception.status_code, 500)


if __name__ == "__main__":
    unittest.main()
