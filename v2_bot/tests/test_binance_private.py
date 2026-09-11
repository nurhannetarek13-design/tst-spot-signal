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


def response(status: int, payload: dict) -> httpx.Response:
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
