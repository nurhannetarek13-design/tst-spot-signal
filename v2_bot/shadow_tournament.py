from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Iterable


KEY_SEPARATOR = "::"
LEGACY_STRATEGY_ID = "LEGACY"


def encode_shadow_key(symbol: str, strategy_id: str) -> str:
    market_symbol = str(symbol or "").strip().upper()
    strategy = str(strategy_id or "").strip()
    if not market_symbol or KEY_SEPARATOR in market_symbol:
        raise ValueError("invalid shadow market symbol")
    if not strategy or KEY_SEPARATOR in strategy:
        raise ValueError("invalid shadow strategy id")
    return f"{market_symbol}{KEY_SEPARATOR}{strategy}"


def decode_shadow_key(value: str) -> tuple[str, str | None]:
    text = str(value or "").strip()
    if KEY_SEPARATOR not in text:
        return text.upper(), None
    symbol, strategy_id = text.split(KEY_SEPARATOR, 1)
    symbol = symbol.strip().upper()
    strategy_id = strategy_id.strip()
    return symbol, strategy_id or None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def summarize_shadow_rows(rows: Iterable[dict[str, Any]]) -> dict[str, float | int | None]:
    counts = {"OPEN": 0, "TP": 0, "SL": 0, "AMBIGUOUS": 0}
    winning_pnls: list[float] = []
    losing_pnls: list[float] = []
    total = 0

    for row in rows:
        total += 1
        status = str(row.get("status", ""))
        counts[status] = counts.get(status, 0) + 1
        pnl_raw = row.get("pnl_usdt")
        if status not in {"TP", "SL"} or pnl_raw is None:
            continue
        pnl = float(pnl_raw)
        if pnl > 0:
            winning_pnls.append(pnl)
        elif pnl < 0:
            losing_pnls.append(pnl)

    wins = counts.get("TP", 0)
    losses = counts.get("SL", 0)
    decisive = wins + losses
    gross_profit = sum(winning_pnls)
    gross_loss_abs = abs(sum(losing_pnls))
    net_pnl = gross_profit - gross_loss_abs
    expectancy = (net_pnl / decisive) if decisive else None
    profit_factor = (gross_profit / gross_loss_abs) if gross_loss_abs > 0 else None
    avg_win = (gross_profit / len(winning_pnls)) if winning_pnls else None
    avg_loss = (sum(losing_pnls) / len(losing_pnls)) if losing_pnls else None
    terminal = decisive + counts.get("AMBIGUOUS", 0)

    return {
        "total": total,
        "open": counts.get("OPEN", 0),
        "wins": wins,
        "losses": losses,
        "ambiguous": counts.get("AMBIGUOUS", 0),
        "decisive": decisive,
        "win_rate": (wins / decisive) if decisive else None,
        "ambiguous_rate": (counts.get("AMBIGUOUS", 0) / terminal) if terminal else None,
        "gross_profit_usdt": round(gross_profit, 8),
        "gross_loss_abs_usdt": round(gross_loss_abs, 8),
        "net_pnl_usdt": round(net_pnl, 8),
        "expectancy_usdt": round(expectancy, 8) if expectancy is not None else None,
        "avg_win_usdt": round(avg_win, 8) if avg_win is not None else None,
        "avg_loss_usdt": round(avg_loss, 8) if avg_loss is not None else None,
        "profit_factor": round(profit_factor, 8) if profit_factor is not None else None,
    }


class StrategyShadowLedgerAdapter:
    """Adds strategy identity to the existing durable shadow ledger without a schema migration.

    The existing ledger primary key is (symbol, signal_open_time).  The adapter stores
    the durable key as SYMBOL::strategy_id, allowing every strategy to track the same
    market candle independently while remaining backward-compatible with legacy rows.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def open_signal(
        self,
        *,
        strategy_id: str,
        symbol: str,
        signal_open_time: float,
        entry_price: float,
        quote_size: float,
        take_profit_pct: float,
        stop_loss_pct: float,
        fee_rate: float,
    ):
        return self.inner.open_signal(
            symbol=encode_shadow_key(symbol, strategy_id),
            signal_open_time=signal_open_time,
            entry_price=entry_price,
            quote_size=quote_size,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            fee_rate=fee_rate,
        )

    def open_outcomes(self):
        return self.inner.open_outcomes()

    def evaluate_closed_candle(self, outcome, candle):
        return self.inner.evaluate_closed_candle(outcome, candle)

    def stats(self):
        return self.inner.stats()

    def _fetch_rows(self) -> list[dict[str, Any]]:
        if hasattr(self.inner, "path"):
            with closing(sqlite3.connect(self.inner.path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT symbol, status, pnl_usdt FROM shadow_outcomes"
                ).fetchall()
            return [dict(row) for row in rows]

        if hasattr(self.inner, "dsn"):
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(self.inner.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
                cur.execute("SELECT symbol, status, pnl_usdt FROM v2.shadow_outcomes")
                rows = cur.fetchall()
            return [dict(row) for row in rows]

        raise RuntimeError("shadow_ledger_backend_not_supported")

    def stats_by_strategy(self) -> dict[str, dict[str, float | int | None]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self._fetch_rows():
            _symbol, strategy_id = decode_shadow_key(str(row.get("symbol", "")))
            grouped[strategy_id or LEGACY_STRATEGY_ID].append(row)
        return {
            strategy_id: summarize_shadow_rows(rows)
            for strategy_id, rows in sorted(grouped.items())
        }


@dataclass(frozen=True)
class ShadowPromotionPolicy:
    min_decisive: int = 60
    min_profit_factor: float = 1.20
    min_expectancy_usdt: float = 0.0
    min_net_pnl_usdt: float = 0.0
    max_ambiguous_rate: float = 0.15

    @classmethod
    def from_env(cls) -> "ShadowPromotionPolicy":
        def env_int(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default

        def env_float(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default

        return cls(
            min_decisive=max(1, env_int("V2_SHADOW_PROMOTION_MIN_TRADES", 60)),
            min_profit_factor=max(0.0, env_float("V2_SHADOW_PROMOTION_MIN_PF", 1.20)),
            min_expectancy_usdt=env_float("V2_SHADOW_PROMOTION_MIN_EXPECTANCY", 0.0),
            min_net_pnl_usdt=env_float("V2_SHADOW_PROMOTION_MIN_NET_PNL", 0.0),
            max_ambiguous_rate=min(
                1.0,
                max(0.0, env_float("V2_SHADOW_PROMOTION_MAX_AMBIGUOUS_RATE", 0.15)),
            ),
        )

    def to_dict(self) -> dict[str, float | int]:
        return {
            "min_decisive": self.min_decisive,
            "min_profit_factor": self.min_profit_factor,
            "min_expectancy_usdt": self.min_expectancy_usdt,
            "min_net_pnl_usdt": self.min_net_pnl_usdt,
            "max_ambiguous_rate": self.max_ambiguous_rate,
        }


def evaluate_shadow_promotion(
    stats: dict[str, Any],
    policy: ShadowPromotionPolicy,
) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    decisive = int(stats.get("decisive", 0) or 0)
    pf = stats.get("profit_factor")
    expectancy = stats.get("expectancy_usdt")
    net_pnl = _number(stats.get("net_pnl_usdt"), 0.0)
    ambiguous_rate = stats.get("ambiguous_rate")

    if decisive < policy.min_decisive:
        blockers.append("shadow_sample_insufficient")
    if pf is None:
        # A no-loss sample can only pass PF once the minimum sample is met.
        if decisive < policy.min_decisive or int(stats.get("losses", 0) or 0) == 0:
            blockers.append("shadow_profit_factor_unproven")
    elif float(pf) < policy.min_profit_factor:
        blockers.append("shadow_profit_factor_below_gate")
    if expectancy is None or float(expectancy) <= policy.min_expectancy_usdt:
        blockers.append("shadow_expectancy_nonpositive")
    if net_pnl <= policy.min_net_pnl_usdt:
        blockers.append("shadow_net_pnl_nonpositive")
    if ambiguous_rate is not None and float(ambiguous_rate) > policy.max_ambiguous_rate:
        blockers.append("shadow_ambiguous_rate_too_high")

    return not blockers, blockers


def rank_shadow_strategies(
    stats_by_strategy: dict[str, dict[str, Any]],
    policy: ShadowPromotionPolicy,
    *,
    allowed_strategy_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for strategy_id, stats in stats_by_strategy.items():
        if strategy_id == LEGACY_STRATEGY_ID:
            continue
        if allowed_strategy_ids is not None and strategy_id not in allowed_strategy_ids:
            continue
        passed, blockers = evaluate_shadow_promotion(stats, policy)
        pf = stats.get("profit_factor")
        effective_pf = 99.0 if pf is None and int(stats.get("wins", 0) or 0) > 0 else _number(pf, 0.0)
        rows.append(
            {
                "strategy_id": strategy_id,
                "promotion_ready": passed,
                "blockers": blockers,
                "stats": stats,
                "_sort": (
                    1 if passed else 0,
                    min(int(stats.get("decisive", 0) or 0), policy.min_decisive),
                    effective_pf,
                    _number(stats.get("expectancy_usdt"), -999.0),
                    _number(stats.get("net_pnl_usdt"), -999.0),
                ),
            }
        )

    rows.sort(key=lambda row: row["_sort"], reverse=True)
    for row in rows:
        row.pop("_sort", None)
    return rows


def first_promotion_candidate(rankings: list[dict[str, Any]]) -> str | None:
    for row in rankings:
        if row.get("promotion_ready"):
            return str(row["strategy_id"])
    return None
