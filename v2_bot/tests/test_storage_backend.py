import unittest
from unittest.mock import patch

from v2_bot.config import Settings
from v2_bot.execution_journal import ExecutionJournal
from v2_bot.shadow_outcomes import ShadowOutcomeLedger
from v2_bot.state import StateStore
from v2_bot.storage_backend import (
    make_execution_journal,
    make_shadow_outcome_ledger,
    make_state_store,
)


class StorageBackendTests(unittest.TestCase):
    def test_default_backend_remains_sqlite(self):
        settings = Settings()
        self.assertEqual(settings.state_backend, "sqlite")
        settings.validate()

    def test_unknown_backend_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "V2_STATE_BACKEND"):
            Settings(state_backend="unknown").validate()

    def test_postgres_backend_requires_database_url(self):
        with self.assertRaisesRegex(RuntimeError, "V2_DATABASE_URL"):
            Settings(state_backend="postgres", database_url="").validate()

    def test_postgres_backend_rejects_non_postgres_url(self):
        with self.assertRaisesRegex(ValueError, "PostgreSQL connection string"):
            Settings(
                state_backend="postgres",
                database_url="https://example.invalid/db",
            ).validate()

    def test_sqlite_factories_keep_existing_implementations(self):
        settings = Settings(state_backend="sqlite", state_db=":memory:")
        self.assertIsInstance(make_state_store(settings), StateStore)
        self.assertIsInstance(make_shadow_outcome_ledger(settings), ShadowOutcomeLedger)
        self.assertIsInstance(make_execution_journal(settings), ExecutionJournal)

    def test_postgres_factories_receive_only_configured_dsn(self):
        dsn = "postgresql://user:pass@example.invalid/v2_state"
        settings = Settings(state_backend="postgres", database_url=dsn)
        with (
            patch("v2_bot.postgres_storage.PostgresStateStore") as state_cls,
            patch("v2_bot.postgres_storage.PostgresShadowOutcomeLedger") as shadow_cls,
            patch("v2_bot.postgres_journal.ExposureAwarePostgresExecutionJournal") as journal_cls,
        ):
            make_state_store(settings)
            make_shadow_outcome_ledger(settings)
            make_execution_journal(settings)

        state_cls.assert_called_once_with(dsn)
        shadow_cls.assert_called_once_with(dsn)
        journal_cls.assert_called_once_with(dsn)


if __name__ == "__main__":
    unittest.main()
