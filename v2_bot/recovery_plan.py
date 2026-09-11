from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .reconciliation import ReconciliationReport


@dataclass(frozen=True)
class RecoveryPlanItem:
    client_order_id: str
    symbol: str
    classification: str
    proposed_action: str
    automation_allowed: bool
    rationale: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "classification": self.classification,
            "proposed_action": self.proposed_action,
            "automation_allowed": self.automation_allowed,
            "rationale": self.rationale,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class RecoveryPlan:
    clear: bool
    items: tuple[RecoveryPlanItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "clear": self.clear,
            "items": [item.to_dict() for item in self.items],
        }


_ACTIONS = {
    "protection_order_list_found": (
        "review_mark_protected",
        "Binance evidence shows an existing protective order list. Verify exact symbol, quantity and list state before any journal transition.",
    ),
    "filled_buy_requires_protection_review": (
        "review_unprotected_exposure",
        "BUY appears filled but protective order-list evidence is absent. Do not place a second BUY; verify balance and open orders before deciding on protection or flattening.",
    ),
    "terminal_zero_fill_candidate_for_abort": (
        "review_mark_aborted",
        "The BUY appears terminal with zero executed quantity. Confirm there is no asset exposure before considering an ABORTED journal transition.",
    ),
    "buy_state_not_confirmed": (
        "manual_order_reconciliation",
        "The BUY cannot be confirmed from exchange evidence. Keep the execution pending and investigate the deterministic client order ID.",
    ),
    "manual_recovery_required": (
        "manual_account_reconciliation",
        "Exchange evidence is insufficient or contradictory. Keep Live blocked until account, balances, orders and journal agree.",
    ),
}


def build_recovery_plan(report: ReconciliationReport) -> RecoveryPlan:
    """Convert read-only exchange evidence into human-review recovery proposals.

    This function never mutates the journal, never calls Binance and never
    authorizes automated recovery. Every proposed action remains review-only.
    """
    if report.pending_count == 0:
        return RecoveryPlan(clear=True, items=())

    items: list[RecoveryPlanItem] = []
    for item in report.items:
        action, rationale = _ACTIONS.get(
            item.classification,
            (
                "manual_account_reconciliation",
                "Unknown reconciliation classification. Keep Live blocked and perform manual review.",
            ),
        )
        items.append(
            RecoveryPlanItem(
                client_order_id=item.client_order_id,
                symbol=item.symbol,
                classification=item.classification,
                proposed_action=action,
                automation_allowed=False,
                rationale=rationale,
                evidence=dict(item.evidence),
            )
        )

    return RecoveryPlan(clear=False, items=tuple(items))
