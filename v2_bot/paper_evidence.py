from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Iterable

import psycopg
from psycopg.rows import dict_row


def summarize_pnls(pnls: Iterable[float]) -> dict[str, float | int | None]:
    values = [float(value) for value in pnls]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    breakeven = len(values) - len(wins) - len(losses)

    gross_profit = sum(wins)
    gross_loss_abs = abs(sum(losses))
    net_pnl = sum(values)
    total = len(values)
    profit_factor = (gross_profit / gross_loss_abs) if gross_loss_abs > 0 else None
    expectancy = (net_pnl / total) if total else None
    avg_win = (gross_profit / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    max_consecutive_losses = 0
    consecutive_losses = 0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        if value < 0:
            consecutive_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
        else:
            consecutive_losses = 0

    return {
        "closed": total,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": breakeven,
        "win_rate": (len(wins) / total) if total else None,
        "gross_profit_usdt": round(gross_profit, 8),
        "gross_loss_abs_usdt": round(gross_loss_abs, 8),
        "net_pnl_usdt": round(net_pnl, 8),
        "expectancy_usdt": round(expectancy, 8) if expectancy is not None else None,
        "avg_win_usdt": round(avg_win, 8) if avg_win is not None else None,
        "avg_loss_usdt": round(avg_loss, 8) if avg_loss is not None else None,
        "profit_factor": round(profit_factor, 8) if profit_factor is not None else None,
        "max_drawdown_usdt": round(max_drawdown, 8),
        "max_consecutive_losses": max_consecutive_losses,
    }


class SqlitePaperEvidence:
    def __init__(self, path: str) -> None:
        self.path = path

    def stats(self) -> dict[str, float | int | None]:
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute(
                "SELECT pnl_usdt FROM trades ORDER BY closed_at ASC, id ASC"
            ).fetchall()
        return summarize_pnls(row[0] for row in rows)


class PostgresPaperEvidence:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn.strip()
        if not self.dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("Postgres paper evidence requires a PostgreSQL DSN")

    def stats(self) -> dict[str, float | int | None]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pnl_usdt FROM v2.trades ORDER BY closed_at ASC, id ASC"
            )
            rows = cur.fetchall()
        return summarize_pnls(float(row["pnl_usdt"]) for row in rows)
