"""Regressions for virtual portfolio bookkeeping and scheduled-scan continuity."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from v2_bot.pionex_ledger_guard import validate_ledger
from v2_bot.pionex_run import run_once
from v2_bot.pionex_safe_cycle import safe_step
from v2_bot.pionex_style import MODES, _buy, _sell, new_state
from v2_bot.tests.test_pionex_style import BOOK, RULES, FakePublic, bars, hourly


class VirtualLedgerIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.states = {mode: new_state(mode, "BTCUSDT", 50) for mode in MODES}

    def validate(self):
        validate_ledger(self.states, symbol="BTCUSDT", budget=50)

    def test_fresh_and_legitimate_closed_trade_conserve_capital(self):
        self.validate()
        state = self.states["fixed_dca"]
        self.assertEqual(_buy(state, 100.0, 15.0, RULES, "test"), "paper_buy")
        self.validate()
        self.assertEqual(_sell(state, 101.0, RULES, "test"), "paper_sell")
        self.validate()
        self.assertAlmostEqual(state["cash"], 50 + state["realized_pnl"])

    def test_phantom_profit_is_rejected(self):
        self.states["spot_grid"]["realized_pnl"] = 10.0
        with self.assertRaisesRegex(RuntimeError, "capital_conservation"):
            self.validate()

    def test_negative_cash_is_rejected(self):
        self.states["trend_breakout"]["cash"] = -10.0
        with self.assertRaisesRegex(RuntimeError, "cash"):
            self.validate()

    def test_nan_balance_is_rejected(self):
        self.states["spot_grid"]["cash"] = float("nan")
        with self.assertRaisesRegex(RuntimeError, "cash"):
            self.validate()

    def test_fabricated_cost_basis_is_rejected(self):
        state = self.states["fixed_dca"]
        _buy(state, 100.0, 15.0, RULES, "test")
        state["lots"][0]["cost"] *= 1.5
        with self.assertRaisesRegex(RuntimeError, "cost_basis"):
            self.validate()

    def test_wrong_budget_is_rejected(self):
        self.states["fixed_dca"]["budget"] = 500.0
        with self.assertRaisesRegex(RuntimeError, "budget"):
            self.validate()

    def test_single_skipped_closed_candle_halts_without_fake_fill(self):
        data = bars()
        state = self.states["spot_grid"]
        state["last_bar"] = int(data[-1]["open_time"]) - 2 * 900_000
        before = state["last_bar"]
        result = safe_step(state, data, hourly(), BOOK, RULES)
        self.assertTrue(result["halted"])
        self.assertEqual(result["action"], "missed_closed_candle_halted_manual_reconciliation")
        self.assertEqual(state["last_bar"], before)
        self.assertEqual(state["trades"], 0)
        self.validate()

    def test_ordinary_one_bar_advance_is_allowed(self):
        data = bars()
        state = self.states["spot_grid"]
        state["last_bar"] = int(data[-1]["open_time"]) - 900_000
        result = safe_step(state, data, hourly(), BOOK, RULES)
        self.assertFalse(result["halted"])
        self.assertEqual(state["last_bar"], int(data[-1]["open_time"]))
        self.validate()

    def test_runner_rejects_tampered_durable_state_and_preserves_file(self):
        fake = FakePublic()
        now = int(fake.candles[-1]["open_time"]) + 900_100
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            run_once(client=fake, path=str(path), now_ms=now)
            payload = json.loads(path.read_text())
            payload["strategies"]["fixed_dca"]["cash"] = 999_999.0
            path.write_text(json.dumps(payload))
            before = path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "capital_conservation"):
                run_once(client=fake, path=str(path), now_ms=now)
            self.assertEqual(path.read_bytes(), before)

    def test_runner_marks_missed_single_bar_halted_without_rewriting_history(self):
        fake = FakePublic()
        now = int(fake.candles[-1]["open_time"]) + 900_100
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            run_once(client=fake, path=str(path), now_ms=now)
            prior = json.loads(path.read_text())
            fake.candles.append({**fake.candles[-1],
                                 "open_time": int(fake.candles[-1]["open_time"]) + 900_000})
            fake.candles.append({**fake.candles[-1],
                                 "open_time": int(fake.candles[-1]["open_time"]) + 900_000})
            now += 2 * 900_000
            report = run_once(client=fake, path=str(path), now_ms=now)
            after = json.loads(path.read_text())
            for mode in MODES:
                self.assertEqual(report["strategies"][mode]["action"],
                                 "missed_closed_candle_halted_manual_reconciliation")
                self.assertEqual(after["strategies"][mode]["last_bar"],
                                 prior["strategies"][mode]["last_bar"])
                self.assertEqual(after["strategies"][mode]["trades"],
                                 prior["strategies"][mode]["trades"])
                self.assertEqual(after["strategies"][mode]["lots"],
                                 prior["strategies"][mode]["lots"])
                self.assertTrue(after["strategies"][mode]["halted"])


if __name__ == "__main__":
    unittest.main()
