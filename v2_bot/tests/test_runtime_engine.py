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


class RuntimeEnginePaperGuardTests(unittest.TestCase):
    def _engine(self, claim_result):
        engine = object.__new__(RuntimeV2Engine)
        engine.settings = Settings(mode="paper")
        engine.state = _State(claim_result)
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


if __name__ == "__main__":
    unittest.main()
