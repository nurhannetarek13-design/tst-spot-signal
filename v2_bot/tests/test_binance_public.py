import unittest

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


if __name__ == "__main__":
    unittest.main()
