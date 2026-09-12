from __future__ import annotations

from .postgres_storage import PostgresExecutionJournal


class ExposureAwarePostgresExecutionJournal(PostgresExecutionJournal):
    """Postgres execution journal with explicit active protected exposure."""

    def active_protected(self):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM v2.execution_journal
                WHERE stage = 'PROTECTED'
                ORDER BY created_at ASC
                """
            )
            rows = cur.fetchall()
        return [self._row(row) for row in rows]

    def has_active_protected(self) -> bool:
        return bool(self.active_protected())
