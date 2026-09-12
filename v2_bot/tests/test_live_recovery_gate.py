import tempfile
import unittest
from pathlib import Path

from v2_bot.config import Settings
from v2_bot.execution_journal import ExecutionJournal
from v2_bot.main import run
from v2_bot.state import StateStore


class _Notifier:
    enabled = False

    def send(self, _text):
        return False


class _FakeEngine:
    def __init__(self, settings):
        self.settings = settings
        self.state = StateStore(settings.state_db)
        self.notifier = _Notifier()
        self.scan_called = False
        self.closed = False

    def scan_once(self):
        self.scan_called = True
        return {"action": None}

    @staticmethod
    def dump_summary(summary):
        return str(summary)

    def close(self):
        self.closed = True


class LiveRecoveryGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "state.sqlite3")
        # Seed a prior deploy marker so the persistence gate is proven when
        # the Live test starts under a different revision.
        StateStore(self.db_path).verify_persistence("rev-old")

    def tearDown(self):
        self.tmp.cleanup()

    def test_pending_execution_blocks_live_before_scan(self):
        ExecutionJournal(self.db_path).begin(
            client_order_id="v2-BTCUSDT-recover",
            symbol="BTCUSDT",
            details={"quote_size": 10},
        )
        settings = Settings(
            mode="live",
            live_trading=True,
            persistent_state=True,
            deploy_revision="rev-new",
            state_db=self.db_path,
        )
        made = []

        def factory(runtime_settings):
            engine = _FakeEngine(runtime_settings)
            made.append(engine)
            return engine

        with self.assertRaisesRegex(RuntimeError, "LIVE_RECOVERY_REQUIRED"):
            run(True, runtime_settings=settings, engine_factory=factory)

        self.assertEqual(len(made), 1)
        self.assertFalse(made[0].scan_called)
        self.assertTrue(made[0].closed)

    def test_terminal_execution_does_not_trigger_recovery_gate(self):
        journal = ExecutionJournal(self.db_path)
        journal.begin(client_order_id="v2-SOLUSDT-done", symbol="SOLUSDT")
        journal.transition("v2-SOLUSDT-done", "BUY_FILLED")
        journal.transition("v2-SOLUSDT-done", "OCO_INTENT")
        journal.transition("v2-SOLUSDT-done", "PROTECTED")
        settings = Settings(
            mode="live",
            live_trading=True,
            persistent_state=True,
            deploy_revision="rev-new",
            state_db=self.db_path,
        )
        made = []

        def factory(runtime_settings):
            engine = _FakeEngine(runtime_settings)
            made.append(engine)
            return engine

        run(True, runtime_settings=settings, engine_factory=factory)
        self.assertTrue(made[0].scan_called)
        self.assertTrue(made[0].closed)


if __name__ == "__main__":
    unittest.main()
