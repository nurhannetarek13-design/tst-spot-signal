import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

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


class FakeState:
    def __init__(self, *, proven=False, reason="first_deploy_marker_created", previous_revision=None):
        self.proven = proven
        self.reason = reason
        self.previous_revision = previous_revision
        self.revisions = []

    def verify_persistence(self, revision):
        self.revisions.append(revision)
        return SimpleNamespace(
            proven=self.proven,
            reason=self.reason,
            previous_revision=self.previous_revision,
            current_revision=revision,
        )


class FakeEngine:
    def __init__(self, _settings, outcomes, *, state=None):
        self.outcomes = list(outcomes)
        self.closed = False
        self.notifier = FakeNotifier()
        self.state = state if state is not None else FakeState()

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

    def test_paper_mode_fails_closed_when_cross_deploy_persistence_is_unproven(self):
        holder = {}
        fake_state = FakeState(proven=False, reason="first_deploy_marker_created")

        def factory(settings):
            engine = FakeEngine(settings, [{"mode": "paper", "ok": True}], state=fake_state)
            holder["engine"] = engine
            return engine

        output = io.StringIO()
        with self.assertRaisesRegex(RuntimeError, "PERSISTENCE_NOT_PROVEN"):
            with redirect_stdout(output):
                run(
                    True,
                    runtime_settings=Settings(
                        mode="paper",
                        persistent_state=True,
                        deploy_revision="rev-a",
                    ),
                    engine_factory=factory,
                    sleep_fn=lambda _seconds: None,
                )

        self.assertEqual(fake_state.revisions, ["rev-a"])
        self.assertIn('"event": "persistence_gate_blocked"', output.getvalue())
        self.assertIn('"persistence_proven": false', output.getvalue())
        self.assertTrue(holder["engine"].closed)

    def test_paper_mode_runs_after_cross_deploy_persistence_is_proven(self):
        holder = {}
        fake_state = FakeState(
            proven=True,
            reason="survived_prior_deployment",
            previous_revision="rev-a",
        )

        def factory(settings):
            engine = FakeEngine(settings, [{"mode": "paper", "ok": True}], state=fake_state)
            holder["engine"] = engine
            return engine

        output = io.StringIO()
        with redirect_stdout(output):
            run(
                True,
                runtime_settings=Settings(
                    mode="paper",
                    persistent_state=True,
                    deploy_revision="rev-b",
                ),
                engine_factory=factory,
                sleep_fn=lambda _seconds: None,
            )

        text = output.getvalue()
        self.assertIn('"persistence_proven": true', text)
        self.assertIn('"persistence_reason": "survived_prior_deployment"', text)
        self.assertTrue(holder["engine"].closed)


if __name__ == "__main__":
    unittest.main()
