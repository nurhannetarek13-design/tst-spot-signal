import tempfile
import unittest
from pathlib import Path

from v2_bot.config import Settings
from v2_bot.engine import V2Engine
from v2_bot.state import StateStore
from v2_bot.strategy import Candidate


class LiveLockMarket:
    def ticker_24h(self):
        return [{"symbol": "TESTUSDT", "quoteVolume": "100000000"}]

    def book_tickers(self):
        return {"TESTUSDT": {"bid": 99.99, "ask": 100.0}}

    def klines(self, _symbol, _interval, _limit):
        return []

    def exchange_info(self, symbol):
        return {
            "symbols": [
                {
                    "symbol": symbol,
                    "status": "TRADING",
                    "isSpotTradingAllowed": True,
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "minPrice": "0.01",
                            "maxPrice": "1000000",
                            "tickSize": "0.01",
                        },
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.001",
                            "maxQty": "100000",
                            "stepSize": "0.001",
                        },
                        {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                    ],
                }
            ]
        }


class NoopNotifier:
    enabled = False

    def send(self, _text):
        raise AssertionError("Live lock test must not send a trade notification")


class LiveEngineHardLockTests(unittest.TestCase):
    def test_even_fully_eligible_live_candidate_cannot_reach_private_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "live-lock.sqlite3")
            engine = object.__new__(V2Engine)
            engine.settings = Settings(
                mode="live",
                live_trading=True,
                persistent_state=True,
                deploy_revision="test-revision",
                state_db=db_path,
                universe_limit=1,
                min_quote_volume_24h=20_000_000.0,
            )
            engine.market = LiveLockMarket()
            engine.state = StateStore(db_path)
            engine.notifier = NoopNotifier()
            candidate = Candidate(
                symbol="TESTUSDT",
                score=100,
                price=100.0,
                signal_open_time=1_000.0,
                previous_20_high=99.5,
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
                pullback=False,
                entry_setup="breakout_retest",
                breakout_retest=True,
                breakout_level=99.5,
            )
            engine._evaluate_symbol = lambda **_kwargs: candidate

            with self.assertRaisesRegex(RuntimeError, "LIVE_EXECUTION_LOCKED"):
                engine.scan_once()


if __name__ == "__main__":
    unittest.main()
