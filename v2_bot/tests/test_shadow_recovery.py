"""Offline tests: no Binance credentials, network, trades or Railway changes."""
import unittest
from types import SimpleNamespace

from v2_bot.shadow_recovery import CANDLE_MS, replay_open_outcome

SIGNAL = 1_800_000_000_000  # valid 15m boundary


class Market:
    def __init__(self, candles):
        self.candles = candles
        self.calls = []

    def klines(self, symbol, interval, limit):
        self.calls.append((symbol, interval, limit))
        return self.candles[-limit:]


class Ledger:
    def __init__(self):
        self.seen = []

    def evaluate_closed_candle(self, outcome, candle):
        self.seen.append(int(candle["open_time"]))
        if candle.get("stop"):
            return SimpleNamespace(status="SL", reason="stop_loss", exit_price=99,
                                   pnl_usdt=-0.1)
        if candle.get("both"):
            return SimpleNamespace(status="AMBIGUOUS", reason="both_hit",
                                   exit_price=None, pnl_usdt=None)
        return outcome


def opened():
    return SimpleNamespace(symbol="BTCUSDT::trend_momentum", signal_open_time=SIGNAL,
                           status="OPEN")


def candle(i, **kwargs):
    return {"open_time": SIGNAL + i * CANDLE_MS, **kwargs}


class ShadowRecoveryTests(unittest.TestCase):
    def test_recovers_stop_after_more_than_eight_candles(self):
        market = Market([candle(i, stop=(i == 3)) for i in range(1, 13)])
        ledger = Ledger()
        event = replay_open_outcome(outcome=opened(), market=market, ledger=ledger,
                                    now_ms=SIGNAL + 13 * CANDLE_MS + 1)
        self.assertEqual(event["status"], "SL")
        self.assertEqual(ledger.seen, [SIGNAL + i * CANDLE_MS for i in (1, 2, 3)])
        self.assertGreater(market.calls[0][2], 8)

    def test_missing_middle_candle_blocks_all_evaluation(self):
        market = Market([candle(1), candle(3, stop=True), candle(4)])
        ledger = Ledger()
        event = replay_open_outcome(outcome=opened(), market=market, ledger=ledger,
                                    now_ms=SIGNAL + 5 * CANDLE_MS + 1)
        self.assertEqual(event["event"], "shadow_outcome_gap_blocked")
        self.assertEqual(ledger.seen, [])

    def test_next_candle_not_closed_does_not_request_data(self):
        market, ledger = Market([]), Ledger()
        event = replay_open_outcome(outcome=opened(), market=market, ledger=ledger,
                                    now_ms=SIGNAL + CANDLE_MS + 20)
        self.assertIsNone(event)
        self.assertEqual(market.calls, [])

    def test_unrecoverable_history_never_fabricates_outcome(self):
        market, ledger = Market([]), Ledger()
        event = replay_open_outcome(outcome=opened(), market=market, ledger=ledger,
                                    now_ms=SIGNAL + 1100 * CANDLE_MS)
        self.assertEqual(event["reason"], "history_exceeds_public_kline_limit")
        self.assertEqual(market.calls, [])
        self.assertEqual(ledger.seen, [])

    def test_ambiguous_exit_remains_ambiguous(self):
        market = Market([candle(1, both=True)])
        ledger = Ledger()
        event = replay_open_outcome(outcome=opened(), market=market, ledger=ledger,
                                    now_ms=SIGNAL + 2 * CANDLE_MS + 1)
        self.assertEqual(event["status"], "AMBIGUOUS")
        self.assertIsNone(event["pnl_usdt_net_fees"])

    def test_non_aligned_signal_is_blocked(self):
        market, ledger = Market([]), Ledger()
        bad = SimpleNamespace(symbol="BTCUSDT", signal_open_time=SIGNAL + 3)
        event = replay_open_outcome(outcome=bad, market=market, ledger=ledger,
                                    now_ms=SIGNAL + 3 * CANDLE_MS)
        self.assertEqual(event["reason"], "invalid_signal_candle_timestamp")
        self.assertEqual(market.calls, [])


if __name__ == "__main__":
    unittest.main()
