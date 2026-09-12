from __future__ import annotations

from typing import Any

from .config import Settings
from .execution_journal import ExecutionJournal
from .paper_evidence import PostgresPaperEvidence, SqlitePaperEvidence
from .shadow_outcomes import ShadowOutcomeLedger
from .state import StateStore


def make_state_store(settings: Settings) -> Any:
    if settings.state_backend == "sqlite":
        return StateStore(settings.state_db)
    if settings.state_backend == "postgres":
        from .postgres_storage import PostgresStateStore

        return PostgresStateStore(settings.database_url)
    raise ValueError(f"unsupported state backend: {settings.state_backend}")


def make_shadow_outcome_ledger(settings: Settings) -> Any:
    if settings.state_backend == "sqlite":
        return ShadowOutcomeLedger(settings.state_db)
    if settings.state_backend == "postgres":
        from .postgres_storage import PostgresShadowOutcomeLedger

        return PostgresShadowOutcomeLedger(settings.database_url)
    raise ValueError(f"unsupported state backend: {settings.state_backend}")


def make_execution_journal(settings: Settings) -> Any:
    if settings.state_backend == "sqlite":
        return ExecutionJournal(settings.state_db)
    if settings.state_backend == "postgres":
        from .postgres_storage import PostgresExecutionJournal

        return PostgresExecutionJournal(settings.database_url)
    raise ValueError(f"unsupported state backend: {settings.state_backend}")


def make_paper_evidence(settings: Settings) -> Any:
    if settings.state_backend == "sqlite":
        return SqlitePaperEvidence(settings.state_db)
    if settings.state_backend == "postgres":
        return PostgresPaperEvidence(settings.database_url)
    raise ValueError(f"unsupported state backend: {settings.state_backend}")
