import unittest
from types import SimpleNamespace
from unittest.mock import patch

from v2_bot.config import Settings
from v2_bot.engine import V2Engine
from v2_bot.runtime_engine import RuntimeV2Engine


class _State:
    def __init__(self, claim_result):
        self.claim_result = claim_result
        self.calls = []

    def claim_signal(self, **kwargs):
        self.calls.append(kwargs)
        return self.claim_result


class _Notifier:
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        return True


class RuntimeEnginePaperGuardTests(unittest.TestCase):
    def _engine(self, claim_result):
        engine = object.__new__(RuntimeV2Engine)
        engine.settings = Settings(mode="paper")
        engine.state = _State(claim_result)
        engine.notifier = _Notifier()
        return engine

    def test_first_paper_signal_remains_allowed_after_preflight(self):
        engine = self._engine(True)
        candidate = SimpleNamespace(symbol="BTCUSDT", signal_open_time=123.0)
        with patch.object(
            V2Engine,
            "_execution_preflight",
            return_value={"allowed": True, "reasons": []},
        ):
            result = engine._execution_preflight(candidate=candidate, books={})
        self.assertTrue(result["allowed"])
        self.assertEqual(
            engine.state.calls,
            [{"symbol": "BTCUSDT", "signal_open_time": 123.0, "kind": "paper"}],
        )

    def test_duplicate_paper_signal_is_blocked_before_open(self):
        engine = self._engine(False)
        candidate = SimpleNamespace(symbol="BTCUSDT", signal_open_time=123.0)
        with patch.object(
            V2Engine,
            "_execution_preflight",
            return_value={"allowed": True, "reasons": []},
        ):
            result = engine._execution_preflight(candidate=candidate, books={})
        self.assertFalse(result["allowed"])
        self.assertIn("paper_signal_duplicate", result["reasons"])

    def test_failed_exchange_preflight_does_not_burn_signal(self):
        engine = self._engine(True)
        candidate = SimpleNamespace(symbol="BTCUSDT", signal_open_time=123.0)
        with patch.object(
            V2Engine,
            "_execution_preflight",
            return_value={"allowed": False, "reasons": ["min_notional"]},
        ):
            result = engine._execution_preflight(candidate=candidate, books={})
        self.assertFalse(result["allowed"])
        self.assertEqual(engine.state.calls, [])

    def test_cumulative_paper_message_contains_key_metrics(self):
        engine = self._engine(True)
        engine._notify_paper_stats(
            {
                "closed": 12,
                "win_rate": 0.625,
                "net_pnl_usdt": 1.2345,
                "expectancy_usdt": 0.102875,
                "profit_factor": 1.42,
                "max_drawdown_usdt": 0.88,
            }
        )
        self.assertEqual(len(engine.notifier.messages), 1)
        message = engine.notifier.messages[0]
        self.assertIn("V2 PAPER CUMULATIVE", message)
        self.assertIn("Closed: 12", message)
        self.assertIn("Win rate: 62.5%", message)
        self.assertIn("Net PnL: 1.2345 USDT", message)
        self.assertIn("Profit factor: 1.4200", message)
        self.assertIn("Max DD: 0.8800 USDT", message)


if __name__ == "__main__":
    unittest.main()
