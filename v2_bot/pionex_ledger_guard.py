"""Fail-closed integrity checks for the isolated, credential-free Spot paper shelf.

The ledger is NOT an exchange account. A restored balance cannot be trusted simply
because its JSON parses: verify holdings, cost basis, capital and fee invariants.
"""
from __future__ import annotations

import math
from typing import Any

from .pionex_style import FEE, MODES

BAR_MS = 900_000


def _number(value: Any, label: str, *, floor: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"corrupt_paper_ledger_{label}")
    result = float(value)
    if not math.isfinite(result) or (floor is not None and result < floor):
        raise RuntimeError(f"corrupt_paper_ledger_{label}")
    return result


def validate_ledger(states: dict[str, Any], *, symbol: str, budget: float) -> None:
    """Reject stale-schema, impossible and internally inconsistent paper balances.

    For every completed buy, cash drops by cost (including entry fee). For every
    completed sale, cash rises by net proceeds and realized PnL rises by proceeds
    minus the sold lot's original cost. Therefore the exact bookkeeping invariant
    is cash + remaining acquisition costs = original budget + realized PnL.
    """
    if not isinstance(states, dict) or set(states) != set(MODES):
        raise RuntimeError("corrupt_paper_ledger_strategy_set")
    for mode in MODES:
        state = states[mode]
        if not isinstance(state, dict) or state.get("schema") != 1:
            raise RuntimeError(f"corrupt_paper_ledger_schema_{mode}")
        if state.get("mode") != mode or state.get("symbol") != symbol:
            raise RuntimeError(f"corrupt_paper_ledger_identity_{mode}")
        configured = _number(state.get("budget"), f"budget_{mode}", floor=0)
        if configured != budget:
            raise RuntimeError(f"corrupt_paper_ledger_budget_{mode}")
        cash = _number(state.get("cash"), f"cash_{mode}", floor=-1e-8)
        realized = _number(state.get("realized_pnl"), f"realized_{mode}")
        paid = _number(state.get("fees_paid"), f"fees_{mode}", floor=0)
        trades = _number(state.get("trades"), f"trades_{mode}", floor=0)
        if not trades.is_integer():
            raise RuntimeError(f"corrupt_paper_ledger_trades_{mode}")
        last = _number(state.get("last_bar"), f"last_bar_{mode}")
        if not last.is_integer() or (int(last) != -1 and (last < 0 or int(last) % BAR_MS)):
            raise RuntimeError(f"corrupt_paper_ledger_last_bar_{mode}")
        if type(state.get("halted")) is not bool:
            raise RuntimeError(f"corrupt_paper_ledger_halt_{mode}")
        anchor = state.get("anchor")
        if anchor is not None:
            _number(anchor, f"anchor_{mode}", floor=1e-12)
        lots = state.get("lots")
        if not isinstance(lots, list) or len(lots) > 2 or len(lots) > int(trades):
            raise RuntimeError(f"corrupt_paper_ledger_lots_{mode}")
        if mode == "trend_breakout" and len(lots) > 1:
            raise RuntimeError("corrupt_paper_ledger_breakout_inventory")
        events = state.get("events")
        if not isinstance(events, list) or len(events) > 100:
            raise RuntimeError(f"corrupt_paper_ledger_events_{mode}")
        total_open_cost = 0.0
        outstanding_entry_fees = 0.0
        for idx, lot in enumerate(lots):
            if not isinstance(lot, dict):
                raise RuntimeError(f"corrupt_paper_ledger_lot_{mode}_{idx}")
            qty = _number(lot.get("qty"), f"qty_{mode}_{idx}", floor=1e-15)
            entry = _number(lot.get("entry"), f"entry_{mode}_{idx}", floor=1e-15)
            cost = _number(lot.get("cost"), f"cost_{mode}_{idx}", floor=1e-15)
            expected = qty * entry * (1 + FEE)
            if not math.isclose(cost, expected, abs_tol=1e-6, rel_tol=1e-8):
                raise RuntimeError(f"corrupt_paper_ledger_cost_basis_{mode}_{idx}")
            total_open_cost += cost
            outstanding_entry_fees += qty * entry * FEE
        if paid + 1e-6 < outstanding_entry_fees:
            raise RuntimeError(f"corrupt_paper_ledger_entry_fees_{mode}")
        if not math.isclose(cash + total_open_cost, budget + realized,
                            abs_tol=1e-5, rel_tol=1e-9):
            raise RuntimeError(f"corrupt_paper_ledger_capital_conservation_{mode}")
