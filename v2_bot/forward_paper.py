from __future__ import annotations

from dataclasses import replace
from typing import Any

from .strategy_pool import PAPER, STRATEGY_SPECS, StrategySpec


FORWARD_PAPER_STRATEGY_ID = "btc_alt_leadlag"
FORWARD_PAPER_STATUS = "FORWARD_PAPER_EXPERIMENT"


def runtime_specs(mode: str) -> tuple[StrategySpec, ...]:
    """Return the execution pool for the requested mode.

    Paper is deliberately allowed to execute exactly one research hypothesis so
    it can collect real forward evidence. This is not a historical promotion and
    never changes Live eligibility.
    """
    if mode != "paper":
        return STRATEGY_SPECS

    return tuple(
        replace(
            spec,
            status=PAPER,
            rationale=(
                f"{spec.rationale} | forward-paper experiment only; historical "
                "discovery was not sufficient for promotion"
            ),
        )
        if spec.strategy_id == FORWARD_PAPER_STRATEGY_ID
        else spec
        for spec in STRATEGY_SPECS
    )


def registry_snapshot_for_mode(mode: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in STRATEGY_SPECS:
        status = spec.status
        if mode == "paper" and spec.strategy_id == FORWARD_PAPER_STRATEGY_ID:
            status = FORWARD_PAPER_STATUS
        rows.append(
            {
                "strategy_id": spec.strategy_id,
                "family": spec.family,
                "status": status,
                "allowed_regimes": list(spec.allowed_regimes),
                "take_profit_pct": spec.take_profit_pct,
                "stop_loss_pct": spec.stop_loss_pct,
                "rationale": spec.rationale,
            }
        )
    return rows
