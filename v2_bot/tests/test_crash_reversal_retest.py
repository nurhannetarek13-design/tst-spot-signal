from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from v2_bot.crash_reversal_retest import (BAR_MS, SYMBOLS, detect, new_state,
    run_all, state_path, step, validate)
from v2_bot.pionex_style import Rules

RULES = Rules(10., .00001, .00001)
BOOK = {"bid": 97.38, "ask": 97.42}
START = 1_800_000_000_000


def candles():
    data = [{"open_time": START + i * BAR_MS, "open": 100., "high": 100.5,
             "low": 99.5, "close": 100., "quote_volume": 1_000_000.}
            for i in range(120)]
    data[-4].update(open=100., high=100.1, low=94.4, close=95.)
    data[-3].update(open=95., high=97.1, low=94.8, close=96.7)
    data[-2].update(open=96.4, high=96.85, low=95.7, close=96.3)
    data[-1].update(open=96.3, high=97.7, low=96.2, close=97.4,
                    quote_volume=1_400_000.)
    return data


class FakePublic:
    def __init__(self):
        self.data = candles()
        self.closed = False

    def exchange_info(self, symbol):
        return {"symbols": [{"symbol": symbol, "status": "TRADING",
            "isSpotTradingAllowed": True,
            "filters": [{"filterType": "LOT_SIZE", "minQty": ".00001", "stepSize": ".00001"},
                        {"filterType": "NOTIONAL", "minNotional": "10"}]}]}

    def klines(self, symbol, interval, limit):
        if interval != "15m":
            raise AssertionError("candidate_must_only_request_spot_15m")
        return self.data

    def book_tickers(self):
        return {symbol: dict(BOOK) for symbol in SYMBOLS}

    def close(self):
        self.closed = True


class CrashRetestPaperTests(unittest.TestCase):
    def test_requires_fully_closed_confirmation_and_all_pattern_parts(self):
        series = candles()
        self.assertIsNotNone(detect(series, **{"ask": BOOK["ask"], "bid": BOOK["bid"]}))
        self.assertIsNone(detect(series[:-1], **{"ask": BOOK["ask"], "bid": BOOK["bid"]}))
        for change in (
            lambda x: x[-4].update(close=99.),
            lambda x: x[-3].update(close=94.5),
            lambda x: x[-2].update(low=94.),
            lambda x: x[-1].update(close=96.7),
            lambda x: x[-1].update(quote_volume=500_000.),
        ):
            changed = copy.deepcopy(series)
            change(changed)
            self.assertIsNone(detect(changed, **{"ask": BOOK["ask"], "bid": BOOK["bid"]}))
        self.assertIsNone(detect(series, ask=99., bid=98.95))  # no chasing a late spike

    def test_no_early_entry_market_book_fill_and_no_duplicate(self):
        data = candles()
        state = new_state("BTCUSDT")
        self.assertEqual(step(state, data[:-1], BOOK, RULES)["trades"], 0)
        entry = step(state, data, BOOK, RULES)
        self.assertEqual(entry["action"], "paper_buy")
        self.assertEqual(entry["actual_orders"], 0)
        self.assertFalse(entry["live_trading"])
        self.assertGreater(state["lots"][0]["entry"], BOOK["ask"])
        self.assertLess(state["stop"], state["lots"][0]["entry"])
        self.assertGreater(state["target"], state["lots"][0]["entry"])
        validate(state, symbol="BTCUSDT", budget=50.)
        again = step(state, data, {"bid": 150., "ask": 150.1}, RULES)
        self.assertEqual(again["action"], "duplicate_bar_no_action")
        self.assertEqual(again["trades"], 1)

    def test_market_target_exit_fee_accounting_not_historical_high_fill(self):
        data = candles()
        state = new_state("BTCUSDT")
        step(state, data, BOOK, RULES)
        target = state["target"]
        next_bar = {**data[-1], "open_time": data[-1]["open_time"] + BAR_MS,
                    "open": target, "high": target + 1., "low": target - .5,
                    "close": target + .25}
        result = step(state, data + [next_bar],
                      {"bid": target + .2, "ask": target + .25}, RULES)
        self.assertEqual(result["action"], "paper_sell")
        self.assertEqual(result["trades"], 2)
        self.assertEqual(len(state["lots"]), 0)
        self.assertIsNone(state["stop"])
        self.assertIsNone(state["target"])
        validate(state, symbol="BTCUSDT", budget=50.)
        self.assertAlmostEqual(state["cash"] - 50, state["realized_pnl"], places=6)

    def test_intrabar_stop_touch_halts_without_fake_exit(self):
        state = new_state("BTCUSDT")
        data = candles()
        step(state, data, BOOK, RULES)
        next_bar = {**data[-1], "open_time": data[-1]["open_time"] + BAR_MS,
                    "open": 97., "high": 98., "low": state["stop"] - .1,
                    "close": 97.5}
        report = step(state, data + [next_bar], BOOK, RULES)
        self.assertEqual(report["action"], "intrabar_stop_touch_halted_no_fabricated_fill")
        self.assertTrue(report["halted"])
        self.assertEqual(report["trades"], 1)
        self.assertEqual(len(state["lots"]), 1)
        validate(state, symbol="BTCUSDT", budget=50.)

    def test_gap_with_position_halts_and_flat_gap_skips_old_signal(self):
        data = candles()
        held = new_state("BTCUSDT")
        step(held, data, BOOK, RULES)
        gap = {**data[-1], "open_time": data[-1]["open_time"] + 2 * BAR_MS}
        result = step(held, data + [gap], BOOK, RULES)
        self.assertTrue(result["halted"])
        self.assertEqual(result["action"], "exposed_gap_halted_no_invented_fill")
        self.assertEqual(result["trades"], 1)
        flat = new_state("ETHUSDT")
        flat["last_bar"] = int(data[-1]["open_time"]) - 2 * BAR_MS
        self.assertEqual(step(flat, data, BOOK, RULES)["action"],
                         "flat_gap_resynchronized_no_trade")
        self.assertEqual(flat["trades"], 0)

    def test_corruption_is_not_reset_to_fifty(self):
        state = new_state("BTCUSDT")
        state["cash"] = 90.
        with self.assertRaisesRegex(RuntimeError, "corrupt_crash_ledger_capital"):
            validate(state, symbol="BTCUSDT", budget=50.)

    def test_three_symbol_forward_run_restores_exact_states(self):
        fake = FakePublic()
        now = int(fake.data[-1]["open_time"]) + BAR_MS + 100
        with tempfile.TemporaryDirectory() as tmp:
            first = run_all(state_dir=tmp, client=fake, now_ms=now)
            self.assertTrue(first["candidate"])
            self.assertEqual(first["real_orders"], 0)
            self.assertFalse(first["live_trading"])
            self.assertTrue(all(v["strategy"]["action"] == "paper_buy"
                                for v in first["by_symbol"].values()))
            self.assertTrue(all(not v["prior_ledger_restored"]
                                for v in first["by_symbol"].values()))
            saved = {symbol: Path(state_path(tmp, symbol)).read_text() for symbol in SYMBOLS}
            again = run_all(state_dir=tmp, client=fake, now_ms=now)
            for symbol in SYMBOLS:
                self.assertTrue(again["by_symbol"][symbol]["prior_ledger_restored"])
                self.assertEqual(again["by_symbol"][symbol]["strategy"]["action"],
                                 "duplicate_bar_no_action")
                self.assertEqual(Path(state_path(tmp, symbol)).read_text(), saved[symbol])
            bad_path = Path(state_path(tmp, "SOLUSDT"))
            tampered = json.loads(bad_path.read_text())
            tampered["cash"] += 10.
            bad_path.write_text(json.dumps(tampered))
            with self.assertRaisesRegex(RuntimeError, "crash_paper_batch_failed_SOLUSDT"):
                run_all(state_dir=tmp, client=fake, now_ms=now)
            self.assertEqual(json.loads(bad_path.read_text())["cash"], tampered["cash"])

    def test_rejects_private_creds_stale_and_fabricated_accounting(self):
        fake = FakePublic()
        now = int(fake.data[-1]["open_time"]) + BAR_MS + 100
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "crash_paper_batch_failed_BTCUSDT"):
                run_all(state_dir=tmp, client=fake, now_ms=now + 4 * BAR_MS)
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
