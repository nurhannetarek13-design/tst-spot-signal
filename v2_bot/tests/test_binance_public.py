import unittest
from unittest.mock import patch

import httpx

from v2_bot.binance_public import BinancePublicClient


def kline(open_time: int, close: float):
    return [
        open_time,
        str(close - 0.1),
        str(close + 0.2),
        str(close - 0.2),
        str(close),
        "10",
        open_time + 899_999,
        "1000",
        100,
        "5.8",
        "580",
        "0",
    ]


class BinancePublicClientTests(unittest.TestCase):
    def test_klines_drop_current_forming_candle_by_default(self):
        client = BinancePublicClient()
        try:
            client._get = lambda *_args, **_kwargs: [
                kline(0, 100.0),
                kline(900_000, 101.0),
                kline(1_800_000, 999.0),
            ]
            candles = client.klines("TESTUSDT", "15m", 3)
        finally:
            client.close()

        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[-1]["close"], 101.0)
        self.assertEqual(candles[-1]["close_time"], 1_799_999.0)

    def test_klines_can_include_current_candle_for_diagnostics_only(self):
        client = BinancePublicClient()
        try:
            client._get = lambda *_args, **_kwargs: [
                kline(0, 100.0),
                kline(900_000, 101.0),
            ]
            candles = client.klines("TESTUSDT", "15m", 2, closed_only=False)
        finally:
            client.close()

        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[-1]["close"], 101.0)

    def test_451_falls_back_to_official_market_data_endpoint(self):
        client = BinancePublicClient(base_urls=("https://api.binance.com", "https://data-api.binance.vision"))
        request_primary = httpx.Request("GET", "https://api.binance.com/api/v3/ticker/24hr")
        request_fallback = httpx.Request("GET", "https://data-api.binance.vision/api/v3/ticker/24hr")
        primary = httpx.Response(451, request=request_primary)
        fallback = httpx.Response(200, request=request_fallback, json=[{"symbol": "BTCUSDT"}])

        try:
            with patch.object(client._client, "get", side_effect=[primary, fallback]) as mocked_get:
                data = client.ticker_24h()
        finally:
            client.close()

        self.assertEqual(data, [{"symbol": "BTCUSDT"}])
        self.assertEqual(mocked_get.call_count, 2)

    def test_rate_limit_does_not_rotate_endpoints(self):
        client = BinancePublicClient(base_urls=("https://api.binance.com", "https://data-api.binance.vision"))
        request = httpx.Request("GET", "https://api.binance.com/api/v3/ticker/24hr")
        response = httpx.Response(429, request=request)

        try:
            with patch.object(client._client, "get", return_value=response) as mocked_get:
                with self.assertRaises(httpx.HTTPStatusError):
                    client.ticker_24h()
        finally:
            client.close()

        self.assertEqual(mocked_get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
