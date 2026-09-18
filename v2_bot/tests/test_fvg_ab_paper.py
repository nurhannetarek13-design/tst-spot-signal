from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from v2_bot.fvg_detector import BAR_MS, bullish_fvg, detect_crash_fvg_retest
from v2_bot.fvg_ab_paper import (SYMBOLS, new_state, run_all, state_path,
                                 step, validate)
from v2_bot.pionex_style import Rules

START = 1_800_000_000_000
BOOK = {"bid": 98.20, "ask": 98.24}
RULES = Rules(10., .00001, .00001)


def bars():
    result = [{"open_time": START+i*BAR_MS, "open": 100., "high": 100.5,
               "low": 99.5, "close": 100., "quote_volume": 1_000_000.}
              for i in range(120)]
    result[-7].update(open=100., high=100.1, low=94.4, close=95.)
    result[-6].update(open=95., high=96.2, low=94.8, close=96.)
    result[-5].update(open=96., high=97.6, low=95.9, close=97.4)
    result[-4].update(open=97.4, high=98.0, low=96.35, close=97.8)
    result[-3].update(open=97.8, high=98., low=96.55, close=97.6)
    result[-2].update(open=97.4, high=97.8, low=96.3, close=97.2)
    result[-1].update(open=97.2, high=98.6, low=97., close=98.2,
                      quote_volume=1_400_000.)
    return result


class FakePublic:
    def __init__(self):
        self.data = bars()
        self.closed = False

    def exchange_info(self, symbol):
        return {"symbols": [{"symbol": symbol, "status": "TRADING",
            "isSpotTradingAllowed": True,
            "filters": [{"filterType": "LOT_SIZE", "minQty": ".00001", "stepSize": ".00001"},
                        {"filterType": "NOTIONAL", "minNotional": "10"}]}]}

    def klines(self, symbol, interval, limit):
        if interval != "15m" or symbol not in SYMBOLS:
            raise AssertionError("unsupported_market_feed")
        return self.data

    def book_tickers(self):
        return {symbol: dict(BOOK) for symbol in SYMBOLS}

    def close(self):
        self.closed = True


class FvgABTests(unittest.TestCase):
    def test_standard_three_bar_gap_and_strict_formation_time(self):
        data = bars()
        zone = bullish_fvg(*data[-6:-3])
        self.assertIsNotNone(zone)
        self.assertEqual(zone["lower"], 96.2)
        self.assertEqual(zone["upper"], 96.35)
        self.assertEqual(zone["formed_bar"], int(data[-4]["open_time"]))
        self.assertIsNone(bullish_fvg(data[-6], data[-5],
            {**data[-4], "low": 96.19}))
        self.assertIsNone(bullish_fvg(data[-6], data[-5],
            {**data[-4], "open_time": data[-4]["open_time"]+BAR_MS}))

    def test_only_closed_confirmation_and_retest_zone_are_eligible(self):
        data = bars()
        good = detect_crash_fvg_retest(data, ask=BOOK["ask"], bid=BOOK["bid"])
        self.assertIsNotNone(good)
        self.assertLess(good["formed_bar"], good["retest_bar"])
        self.assertLess(good["retest_bar"], good["confirm_bar"])
        self.assertLess(good["stop"], good["entry"])
        self.assertGreater(good["target"], good["entry"])
        self.assertIsNone(detect_crash_fvg_retest(data[:-1], ask=BOOK["ask"], bid=BOOK["bid"]))
        for change in (
            lambda x: x[-7].update(close=99.),
            lambda x: x[-6].update(close=94.8),
            lambda x: x[-4].update(low=96.19),
            lambda x: x[-3].update(low=96.1),
            lambda x: x[-2].update(low=96.1),
            lambda x: x[-1].update(close=97.7),
            lambda x: x[-1].update(quote_volume=200_000.),
        ):
            altered = copy.deepcopy(data)
            change(altered)
            self.assertIsNone(detect_crash_fvg_retest(altered,
                ask=BOOK["ask"], bid=BOOK["bid"])))
        self.assertIsNone(detect_crash_fvg_retest(data, ask=99., bid=98.95))

    def test_actual_book_paper_entry_and_idempotent_repeat(self):
        data = bars()
        state = new_state("BTCUSDT")
        self.assertEqual(step(state, data[:-1], BOOK, RULES)["trades"], 0)
        entry = step(state, data, BOOK, RULES)
        self.assertEqual(entry["action"], "paper_buy")
        self.assertEqual(entry["actual_orders"], 0)
        self.assertFalse(entry["live_trading"])
        self.assertGreater(state["lots"][0]["entry"], BOOK["ask"])
        self.assertGreater(state["fees_paid"], 0)
        self.assertLess(state["last_signal"]["formed_bar"], state["last_signal"]["confirm_bar"])
        self.assertEqual(state["events"][-1]["signal"]["lower"], 96.2)
        validate(state, symbol="BTCUSDT", budget=50.)
        duplicate = step(state, data, {"bid": 150., "ask": 150.1}, RULES)
        self.assertEqual(duplicate["action"], "duplicate_bar_no_action")
        self.assertEqual(duplicate["trades"], 1)

    def test_fee_aware_observed_target_exit_and_trade_audit(self):
        data, state = bars(), new_state("BTCUSDT")
        step(state, data, BOOK, RULES)
        target = state["target"]
        later = {**data[-1], "open_time": data[-1]["open_time"]+BAR_MS,
                 "open": target, "high": target+1., "low": target-.3,
                 "close": target+.2}
        result = step(state, data+[later],
                      {"bid": target+.2, "ask": target+.25}, RULES)
        self.assertEqual(result["action"], "paper_sell")
        self.assertEqual(result["trades"], 2)
        self.assertEqual(len(state["lots"]), 0)
        self.assertIsNone(state["stop"])
        self.assertAlmostEqual(state["cash"]-50., state["realized_pnl"], places=6)
        self.assertAlmostEqual(state["events"][-1]["closed_trade_net_pnl_usdt"],
                               state["realized_pnl"], places=6)
        validate(state, symbol="BTCUSDT", budget=50.)

    def test_intrabar_stop_touch_does_not_invent_sell(self):
        data, state = bars(), new_state("BTCUSDT")
        step(state, data, BOOK, RULES)
        later = {**data[-1], "open_time": data[-1]["open_time"]+BAR_MS,
                 "low": state["stop"]-.1, "close": 98.2}
        report = step(state, data+[later], BOOK, RULES)
        self.assertEqual(report["action"], "intrabar_stop_touch_halted_no_fabricated_fill")
        self.assertTrue(report["halted"])
        self.assertEqual(report["trades"], 1)
        self.assertEqual(len(state["lots"]), 1)
        validate(state, symbol="BTCUSDT", budget=50.)

    def test_exit_filter_rejects_future_untradeable_stop(self):
        state = new_state("BTCUSDT")
        result = step(state, bars(), BOOK, Rules(10., .1, .1))
        self.assertEqual(result["action"], "stop_exit_would_violate_exchange_minimum")
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["equity_usdt"], 50.)

    def test_exposed_gap_halts_flat_gap_skips_signal(self):
        data, state = bars(), new_state("BTCUSDT")
        step(state, data, BOOK, RULES)
        gap = {**data[-1], "open_time": data[-1]["open_time"] + 2*BAR_MS}
        result = step(state, data+[gap], BOOK, RULES)
        self.assertTrue(result["halted"])
        self.assertEqual(result["action"], "exposed_gap_halted_no_invented_fill")
        self.assertEqual(result["trades"], 1)
        flat = new_state("ETHUSDT")
        flat["last_bar"] = int(data[-1]["open_time"])-2*BAR_MS
        self.assertEqual(step(flat, data, BOOK, RULES)["action"],
                         "flat_gap_resynchronized_no_trade")
        self.assertEqual(flat["trades"], 0)

    def test_same_snapshot_six_ledgers_restore_and_no_false_profit(self):
        fake = FakePublic()
        now = int(fake.data[-1]["open_time"]) + BAR_MS + 100
        with tempfile.TemporaryDirectory() as tmp:
            result = run_all(state_dir=tmp, client=fake, now_ms=now)
            self.assertFalse(result["live_trading"])
            self.assertEqual(result["real_orders"], 0)
            self.assertTrue(result["six_independent_alternative_scenarios_do_not_sum_capital"])
            self.assertEqual(set(result["by_symbol"]), set(SYMBOLS))
            for symbol in SYMBOLS:
                self.assertEqual(result["by_symbol"][symbol]["fvg"]["action"], "paper_buy")
                self.assertEqual(result["by_symbol"][symbol]["baseline"]["trades"], 0)
                self.assertEqual(result["by_symbol"][symbol]["fvg"]["trades"], 1)
            before = {str(p): p.read_bytes() for p in Path(tmp).glob("*.json")}
            self.assertEqual(len(before), 6)
            repeat = run_all(state_dir=tmp, client=fake, now_ms=now)
            self.assertTrue(all(v["both_variants_restored"] for v in repeat["by_symbol"].values()))
            self.assertTrue(all(v[m]["action"] == "duplicate_bar_no_action"
                for v in repeat["by_symbol"].values() for m in ("baseline", "fvg")))
            self.assertEqual(before, {str(p): p.read_bytes() for p in Path(tmp).glob("*.json")})
            Path(state_path(tmp, "SOLUSDT", "fvg")).unlink()
            with self.assertRaisesRegex(RuntimeError, "partial_fvg_ab_state"):
                run_all(state_dir=tmp, client=fake, now_ms=now)

    def test_corrupt_ledgers_and_stale_market_fail_closed(self):
        fake = FakePublic()
        now = int(fake.data[-1]["open_time"]) + BAR_MS + 100
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "stale_fvg_ab_data"):
                run_all(state_dir=tmp, client=fake, now_ms=now+5*BAR_MS)
            self.assertEqual(list(Path(tmp).glob("*.json")), [])
            run_all(state_dir=tmp, client=fake, now_ms=now)
            bad = Path(state_path(tmp, "ETHUSDT", "fvg"))
            row = json.loads(bad.read_text())
            row["cash"] += 100
            bad.write_text(json.dumps(row))
            with self.assertRaisesRegex(RuntimeError, "corrupt_crash_ledger_capital"):
                run_all(state_dir=tmp, client=fake, now_ms=now)
            self.assertEqual(json.loads(bad.read_text())["cash"], row["cash"])

    def test_no_private_credentials(self):
        with patch.dict("os.environ", {"BINANCE_API_KEY": "must-not-be-used"}):
            with self.assertRaisesRegex(RuntimeError, "private_exchange_credentials_forbidden"):
                run_all(client=FakePublic())


if __name__ == "__main__":
    unittest.main()
