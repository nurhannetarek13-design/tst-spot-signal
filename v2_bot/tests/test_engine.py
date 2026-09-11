import tempfile
import unittest
from pathlib import Path

from v2_bot.config import Settings
from v2_bot.engine import V2Engine
from v2_bot.state import StateStore
from v2_bot.strategy import Candidate


class FakeMarket:
    def __init__(self):
        self.books = {"TESTUSDT": {"bid": 99.99, "ask": 100.0}}

    def ticker_24h(self):
        return [{"symbol": "TESTUSDT", "quoteVolume": "100000000"}]

    def book_tickers(self):
        return self.books

    def klines(self, _symbol, _interval, _limit):
        return []


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        return True


def eligible_candidate(signal_open_time=1_000.0):
    return Candidate(
        symbol="TESTUSDT",
        score=100,
        price=100.0,
        signal_open_time=signal_open_time,
        previous_20_high=99.0,
        relative_volume=2.0,
        taker_buy_ratio=0.60,
        spread_bps=1.0,
        quote_volume_24h=100_000_000.0,
        btc_regime_ok=True,
        trend_15m=True,
        trend_1h=True,
        trend_4h=True,
        breakout=True,
        rel_volume_ok=True,
        taker_flow_ok=True,
        eligible=True,
    )


class EngineUniverseTests(unittest.TestCase):
    def make_engine(self):
        engine = object.__new__(V2Engine)
        engine.settings = Settings(universe_limit=10, min_quote_volume_24h=20_000_000.0)
        return engine

    def test_excludes_stablecoin_and_fiat_base_pairs(self):
        engine = self.make_engine()
        tickers = [
            {"symbol": "BTCUSDT", "quoteVolume": "1000000000"},
            {"symbol": "USDCUSDT", "quoteVolume": "900000000"},
            {"symbol": "FDUSDUSDT", "quoteVolume": "800000000"},
            {"symbol": "EURUSDT", "quoteVolume": "700000000"},
            {"symbol": "SOLUSDT", "quoteVolume": "600000000"},
        ]
        universe = engine._build_universe(tickers)
        symbols = [symbol for symbol, _ in universe]

        self.assertIn("BTCUSDT", symbols)
        self.assertIn("SOLUSDT", symbols)
        self.assertNotIn("USDCUSDT", symbols)
        self.assertNotIn("FDUSDUSDT", symbols)
        self.assertNotIn("EURUSDT", symbols)

    def test_keeps_volume_filter_and_ranking(self):
        engine = self.make_engine()
        tickers = [
            {"symbol": "SOLUSDT", "quoteVolume": "600000000"},
            {"symbol": "ETHUSDT", "quoteVolume": "1000000000"},
            {"symbol": "TESTUSDT", "quoteVolume": "1000000"},
        ]
        universe = engine._build_universe(tickers)
        self.assertEqual(universe, [("ETHUSDT", 1_000_000_000.0), ("SOLUSDT", 600_000_000.0)])


class EngineLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "engine.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def make_engine(self, mode):
        engine = object.__new__(V2Engine)
        engine.settings = Settings(
            mode=mode,
            live_trading=False,
            state_db=self.db_path,
            universe_limit=10,
            min_quote_volume_24h=20_000_000.0,
        )
        engine.market = FakeMarket()
        engine.state = StateStore(self.db_path)
        engine.notifier = FakeNotifier()
        engine.current_candidate = eligible_candidate()
        engine._evaluate_symbol = lambda **_kwargs: engine.current_candidate
        return engine

    def test_shadow_signal_is_emitted_once_per_closed_candle(self):
        engine = self.make_engine("shadow")

        first = engine.scan_once()
        second = engine.scan_once()

        self.assertEqual(first["action"]["event"], "shadow_signal")
        self.assertEqual(second["action"]["event"], "shadow_duplicate_suppressed")
        self.assertEqual(len(engine.notifier.messages), 1)

        engine.current_candidate = eligible_candidate(signal_open_time=2_000.0)
        third = engine.scan_once()
        self.assertEqual(third["action"]["event"], "shadow_signal")
        self.assertEqual(len(engine.notifier.messages), 2)

    def test_paper_tp_close_does_not_reopen_same_symbol_in_same_cycle(self):
        engine = self.make_engine("paper")

        opened = engine.scan_once()
        self.assertEqual(opened["action"]["event"], "paper_open")
        self.assertEqual(opened["open_positions"], 1)

        engine.market.books["TESTUSDT"] = {"bid": 101.0, "ask": 101.01}
        closed = engine.scan_once()

        self.assertEqual(len(closed["paper_events"]), 1)
        self.assertEqual(closed["paper_events"][0]["reason"], "take_profit")
        self.assertEqual(closed["action"]["event"], "blocked")
        self.assertEqual(closed["action"]["reason"], "eligible_symbols_already_open_or_just_closed")
        self.assertEqual(closed["open_positions"], 0)
        self.assertGreater(closed["realized_pnl_today"], 0)


if __name__ == "__main__":
    unittest.main()
