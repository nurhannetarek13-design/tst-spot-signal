import io
import unittest
from contextlib import redirect_stdout

import httpx

from v2_bot.config import Settings
from v2_bot.main import run


class FakeNotifier:
    def __init__(self):
        self.messages = []
        self.enabled = True

    def send(self, text):
        self.messages.append(text)
        return True


class FakeEngine:
    def __init__(self, _settings, outcomes):
        self.outcomes = list(outcomes)
        self.closed = False
        self.notifier = FakeNotifier()

    def scan_once(self):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @staticmethod
    def dump_summary(summary):
        return str(summary)

    def close(self):
        self.closed = True


class MainLoopTests(unittest.TestCase):
    def test_continuous_mode_retries_after_transient_http_error(self):
        request = httpx.Request("GET", "https://api.binance.com/api/v3/ticker/24hr")
        error = httpx.ConnectError("temporary", request=request)
        holder = {}

        def factory(settings):
            engine = FakeEngine(settings, [error, {"mode": "shadow", "ok": True}])
            holder["engine"] = engine
            return engine

        sleeps = []
        output = io.StringIO()
        with redirect_stdout(output):
            run(
                False,
                runtime_settings=Settings(scan_interval_seconds=60),
                engine_factory=factory,
                sleep_fn=sleeps.append,
                max_cycles=2,
            )

        text = output.getvalue()
        self.assertIn('"event": "market_data_error"', text)
        self.assertIn("'ok': True", text)
        self.assertEqual(sleeps, [60])
        self.assertTrue(holder["engine"].closed)

    def test_once_mode_fails_loudly_on_http_error(self):
        request = httpx.Request("GET", "https://api.binance.com/api/v3/ticker/24hr")
        error = httpx.ConnectError("temporary", request=request)
        holder = {}

        def factory(settings):
            engine = FakeEngine(settings, [error])
            holder["engine"] = engine
            return engine

        with self.assertRaises(httpx.ConnectError):
            with redirect_stdout(io.StringIO()):
                run(
                    True,
                    runtime_settings=Settings(),
                    engine_factory=factory,
                    sleep_fn=lambda _seconds: None,
                )
        self.assertTrue(holder["engine"].closed)

    def test_startup_alert_is_sent_when_requested(self):
        holder = {}

        def factory(settings):
            engine = FakeEngine(settings, [{"mode": "shadow", "ok": True}])
            holder["engine"] = engine
            return engine

        output = io.StringIO()
        with redirect_stdout(output):
            run(
                True,
                runtime_settings=Settings(mode="shadow", live_trading=False, startup_alert=True),
                engine_factory=factory,
                sleep_fn=lambda _seconds: None,
            )

        self.assertEqual(
            holder["engine"].notifier.messages,
            ["V2 SHADOW ONLINE\nLive trading: OFF"],
        )
        self.assertIn('"startup_alert_sent": true', output.getvalue())
        self.assertTrue(holder["engine"].closed)


if __name__ == "__main__":
    unittest.main()
