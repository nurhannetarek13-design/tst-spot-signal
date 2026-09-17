from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from v2_bot.pionex_run import run_once
from v2_bot.pionex_style import MODES, Rules, _buy, _sell, new_state, step


def bars(n=120, width=900_000, start=1_800_000_000_000, price=100.0):
    return [{"open_time": start + i * width, "open": price,
             "high": price * 1.03, "low": price * 0.97, "close": price,
             "quote_volume": 1_000_000.0, "taker_buy_quote": 600_000.0}
            for i in range(n)]


def hourly():
    rows = bars(width=3_600_000)
    for i, row in enumerate(rows):
        row["open"] = 99.8 + i * .002
        row["close"] = 99.8 + i * .002
        row["high"] = row["close"] * 1.03
        row["low"] = row["close"] * .97
    return rows


RULES = Rules(10.0, 0.00001, 0.00001)
BOOK = {"bid": 99.95, "ask": 100.0}


class FakePublic:
    def __init__(self):
        self.candles = bars()
        self.hourly = hourly()

    def exchange_info(self, symbol):
        return {"symbols": [{"symbol": symbol, "status": "TRADING",
                             "isSpotTradingAllowed": True,
                             "filters": [{"filterType": "LOT_SIZE", "minQty": "0.00001", "stepSize": "0.00001"},
                                         {"filterType": "NOTIONAL", "minNotional": "10"}]}]}

    def klines(self, symbol, interval, limit):
        return self.candles if interval == "15m" else self.hourly

    def book_tickers(self):
        return {"BTCUSDT": BOOK}


class StrategyShelfTests(unittest.TestCase):
    def test_three_modes_and_independent_virtual_budget(self):
        states = [new_state(mode, "BTCUSDT", 50) for mode in MODES]
        self.assertEqual(len(states), 3)
        self.assertEqual(sum(s["cash"] for s in states), 150)  # independent scenarios, not shared portfolio
        for s in states:
            self.assertEqual(s["budget"], 50)
            self.assertEqual(s["trades"], 0)

    def test_exchange_minimum_checks_buy_and_sell(self):
        state = new_state("spot_grid", "BTCUSDT")
        self.assertEqual(_buy(state, 100, 8, RULES, "test"), "buy_under_exchange_minimum")
        self.assertEqual(state["cash"], 50)
        self.assertEqual(_buy(state, 100, 15, RULES, "test"), "paper_buy")
        self.assertEqual(_sell(state, 100, Rules(30, .00001, .00001), "test"),
                         "sell_under_exchange_minimum_do_not_fake_fill")
        self.assertEqual(len(state["lots"]), 1)

    def test_grid_waits_then_buys_and_sells_once_per_bar(self):
        state = new_state("spot_grid", "BTCUSDT")
        data = bars()
        first = step(state, data, hourly(), BOOK, RULES)
        self.assertEqual(first["action"], "grid_initialized_without_order")
        data.append({**data[-1], "open_time": data[-1]["open_time"] + 900_000, "close": 98.5})
        buy = step(state, data, hourly(), {"bid": 98.45, "ask": 98.5}, RULES)
        self.assertEqual(buy["action"], "paper_buy")
        self.assertEqual(len(state["lots"]), 1)
        duplicate = step(state, data, hourly(), {"bid": 101., "ask": 101.05}, RULES)
        self.assertEqual(duplicate["action"], "duplicate_bar_no_action")
        self.assertEqual(state["trades"], 1)
        data.append({**data[-1], "open_time": data[-1]["open_time"] + 900_000})
        sell = step(state, data, hourly(), {"bid": 100.5, "ask": 100.55}, RULES)
        self.assertEqual(sell["action"], "paper_sell")
        self.assertGreater(state["realized_pnl"], 0)

    def test_dca_fixed_size_without_martingale(self):
        state = new_state("fixed_dca", "BTCUSDT")
        data = bars()
        data[-1]["close"] = 100
        first = step(state, data, hourly(), BOOK, RULES)
        self.assertEqual(first["action"], "paper_buy")
        first_cost = state["lots"][0]["cost"]
        data.append({**data[-1], "open_time": data[-1]["open_time"] + 900_000, "close": 97.0})
        second = step(state, data, hourly(), {"bid": 97.0, "ask": 97.05}, RULES)
        self.assertEqual(second["action"], "paper_buy")
        self.assertAlmostEqual(state["lots"][1]["cost"], first_cost, delta=.01)
        self.assertEqual(len(state["lots"]), 2)

    def test_loss_cap_exits_and_halts(self):
        state = new_state("trend_breakout", "BTCUSDT")
        _buy(state, 100, 15, RULES, "test")
        outcome = step(state, bars(), hourly(), {"bid": 70, "ask": 70.05}, RULES)
        self.assertTrue(outcome["halted"])
        self.assertEqual(outcome["action"], "paper_sell_halted")
        self.assertEqual(len(state["lots"]), 0)
        self.assertLess(outcome["total_pnl_including_unrealized_usdt"], -2)

    def test_loss_cap_does_not_claim_illegal_dust_sale(self):
        state = new_state("trend_breakout", "BTCUSDT")
        _buy(state, 100, 15, RULES, "test")
        outcome = step(state, bars(), hourly(), {"bid": 65, "ask": 65.05}, RULES)
        self.assertTrue(outcome["halted"])
        self.assertEqual(outcome["action"], "sell_under_exchange_minimum_do_not_fake_fill_halted")
        self.assertEqual(len(state["lots"]), 1)

    def test_gap_halts_without_fake_fill(self):
        state = new_state("fixed_dca", "BTCUSDT")
        state["last_bar"] = int(bars()[-1]["open_time"]) - 4 * 900_000
        result = step(state, bars(), hourly(), BOOK, RULES)
        self.assertTrue(result["halted"])
        self.assertEqual(result["trades"], 0)

    def test_budget_not_spread_across_unfunded_tranches(self):
        result = step(new_state("spot_grid", "BTCUSDT", 20), bars(), hourly(), BOOK, RULES)
        self.assertEqual(result["action"], "insufficient_budget_for_two_safe_tranches")

    def test_runner_reads_public_only_saves_restores_idempotently(self):
        fake = FakePublic()
        with tempfile.TemporaryDirectory() as tmp:
            file = str(Path(tmp) / "state.json")
            now = int(fake.candles[-1]["open_time"]) + 900_100
            first = run_once(client=fake, path=file, now_ms=now)
            self.assertFalse(first["prior_ledger_restored"])
            self.assertEqual(first["real_orders"], 0)
            self.assertEqual(set(first["strategies"]), set(MODES))
            second = run_once(client=fake, path=file, now_ms=now)
            self.assertTrue(second["prior_ledger_restored"])
            self.assertTrue(all(v["action"] == "duplicate_bar_no_action"
                                for v in second["strategies"].values()))

    def test_runner_stale_data_fails_closed(self):
        fake = FakePublic()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "stale_or_future"):
                run_once(client=fake, path=str(Path(tmp) / "state.json"),
                         now_ms=int(fake.candles[-1]["open_time"]) + 4 * 900_000)

    def test_runner_missing_candle_fails_closed(self):
        fake = FakePublic()
        fake.candles[-3]["open_time"] -= 1
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "missing_or_duplicate"):
                run_once(client=fake, path=str(Path(tmp) / "state.json"),
                         now_ms=int(fake.candles[-1]["open_time"]) + 900_100)


if __name__ == "__main__":
    unittest.main()
