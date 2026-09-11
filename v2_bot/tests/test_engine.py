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


def eligible_candidate(signal_open_time=1_000.0, *, setup="breakout_retest"):
    is_retest = setup == "breakout_retest"
    is_pullback = setup == "pullback"
    return Candidate(
        symbol="TESTUSDT",
        score=100,
        price=100.0,
        signal_open_time=signal_open_time,
        previous_20_high=101.0,
        relative_volume=2.0,
        taker_buy_ratio=0.60,
        spread_bps=1.0,
        quote_volume_24h=100_000_000.0,
        btc_regime_ok=True,
        trend_15m=True,
        trend_1h=True,
        trend_4h=True,
        breakout=False,
        rel_volume_ok=True,
        taker_flow_ok=True,
        eligible=True,
        pullback=is_pullback,
        entry_setup=setup,
        breakout_retest=is_retest,
        breakout_level=99.5 if is_retest else 0.0,
    )


def raw_breakout_candidate(signal_open_time=3_000.0, *, trend_1h=True):
    return Candidate(
        symbol="TESTUSDT",
        score=85 if trend_1h else 75,
        price=102.0,
        signal_open_time=signal_open_time,
        previous_20_high=101.0,
        relative_volume=2.0,
        taker_buy_ratio=0.60,
        spread_bps=1.0,
        quote_volume_24h=100_000_000.0,
        btc_regime_ok=True,
        trend_15m=True,
        trend_1h=trend_1h,
        trend_4h=True,
        breakout=True,
        rel_volume_ok=True,
        taker_flow_ok=True,
        eligible=False,
        pullback=False,
        entry_setup="none",
        breakout_retest=False,
        breakout_level=101.0,
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

    def test_excludes_non_ascii_symbols_but_keeps_numeric_assets(self):
        engine = self.make_engine()
        tickers = [
            {"symbol": "牛来USDT", "quoteVolume": "2000000000"},
            {"symbol": "1000PEPEUSDT", "quoteVolume": "1000000000"},
            {"symbol": "BTCUSDT", "quoteVolume": "900000000"},
        ]
        universe = engine._build_universe(tickers)
        symbols = [symbol for symbol, _ in universe]

        self.assertNotIn("牛来USDT", symbols)
        self.assertIn("1000PEPEUSDT", symbols)
        self.assertIn("BTCUSDT", symbols)

    def test_keeps_volume_filter_and_ranking(self):
        engine = self.make_engine()
        tickers = [
            {"symbol": "SOLUSDT", "quoteVolume": "600000000"},
            {"symbol": "ETHUSDT", "quoteVolume": "1000000000"},
            {"symbol": "TESTUSDT", "quoteVolume": "1000000"},
        ]
        universe = engine._build_universe(tickers)
        self.assertEqual(universe, [("ETHUSDT", 1_000_000_000.0), ("SOLUSDT", 600_000_000.0)])

    def test_gate_failure_diagnostics_are_explicit(self):
        settings = Settings(max_spread_bps=15.0, min_score=90)
        candidate = Candidate(
            symbol="TESTUSDT",
            score=70,
            price=100.0,
            signal_open_time=1_000.0,
            previous_20_high=101.0,
            relative_volume=1.0,
            taker_buy_ratio=0.50,
            spread_bps=20.0,
            quote_volume_24h=100_000_000.0,
            btc_regime_ok=False,
            trend_15m=True,
            trend_1h=False,
            trend_4h=True,
            breakout=True,
            rel_volume_ok=False,
            taker_flow_ok=False,
            eligible=False,
            pullback=False,
            entry_setup="none",
            breakout_retest=False,
        )

        failed = V2Engine._failed_gates(candidate, settings)
        self.assertEqual(
            failed,
            [
                "btc_regime",
                "trend_1h",
                "entry_setup",
                "relative_volume",
                "taker_flow",
                "spread",
                "score",
            ],
        )
        counts = V2Engine._gate_failure_counts([candidate], settings)
        self.assertEqual(counts["btc_regime"], 1)
        self.assertEqual(counts["trend_15m"], 0)
        self.assertEqual(counts["trend_1h"], 1)
        self.assertEqual(counts["entry_setup"], 1)
        self.assertEqual(counts["relative_volume"], 1)
        self.assertEqual(counts["taker_flow"], 1)
        self.assertEqual(counts["spread"], 1)
        self.assertEqual(counts["score"], 1)

    def test_breakout_retest_satisfies_entry_setup_gate(self):
        settings = Settings(max_spread_bps=15.0, min_score=90)
        candidate = eligible_candidate(setup="breakout_retest")
        self.assertNotIn("entry_setup", V2Engine._failed_gates(candidate, settings))

    def test_pullback_satisfies_entry_setup_gate(self):
        settings = Settings(max_spread_bps=15.0, min_score=90)
        candidate = eligible_candidate(setup="pullback")
        self.assertNotIn("entry_setup", V2Engine._failed_gates(candidate, settings))

    def test_prealert_requires_every_pre_entry_quality_gate(self):
        settings = Settings(max_spread_bps=15.0, min_score=90)
        self.assertTrue(V2Engine._is_breakout_prealert(raw_breakout_candidate(), settings))
        self.assertFalse(
            V2Engine._is_breakout_prealert(raw_breakout_candidate(trend_1h=False), settings)
        )


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
            persistent_state=True if mode == "paper" else False,
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
        self.assertIsNone(first["pre_alert"])
        self.assertEqual(first["action"]["entry_setup"], "breakout_retest")
        self.assertIn("Setup: breakout_retest", engine.notifier.messages[0])
        self.assertEqual(second["action"]["event"], "shadow_duplicate_suppressed")
        self.assertEqual(len(engine.notifier.messages), 1)

        engine.current_candidate = eligible_candidate(signal_open_time=2_000.0, setup="pullback")
        third = engine.scan_once()
        self.assertEqual(third["action"]["event"], "shadow_signal")
        self.assertEqual(third["action"]["entry_setup"], "pullback")
        self.assertIn("Setup: pullback", engine.notifier.messages[-1])
        self.assertEqual(len(engine.notifier.messages), 2)

    def test_breakout_prealert_is_no_entry_and_deduplicated(self):
        engine = self.make_engine("shadow")
        engine.current_candidate = raw_breakout_candidate()

        first = engine.scan_once()
        second = engine.scan_once()

        self.assertIsNone(first["action"])
        self.assertEqual(first["eligible"], 0)
        self.assertEqual(first["pre_alert"]["event"], "breakout_prealert")
        self.assertIn("NO ENTRY", engine.notifier.messages[0])
        self.assertEqual(second["pre_alert"]["event"], "breakout_prealert_duplicate_suppressed")
        self.assertEqual(len(engine.notifier.messages), 1)

    def test_paper_tp_close_does_not_reopen_same_symbol_in_same_cycle(self):
        engine = self.make_engine("paper")

        opened = engine.scan_once()
        self.assertEqual(opened["action"]["event"], "paper_open")
        self.assertEqual(opened["action"]["setup"], "breakout_retest")
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
