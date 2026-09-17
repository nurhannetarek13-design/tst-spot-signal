from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .strategy import Candidate


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


def check_risk(
    *,
    settings: Settings,
    candidate: Candidate,
    realized_pnl_today: float,
    open_positions: int,
) -> RiskDecision:
    if not candidate.eligible:
        return RiskDecision(False, "signal_not_eligible")
    if realized_pnl_today <= -abs(settings.max_daily_loss_usdt):
        return RiskDecision(False, "daily_loss_limit_reached")
    if open_positions >= settings.max_open_positions:
        return RiskDecision(False, "max_open_positions_reached")
    if candidate.score < settings.min_score:
        return RiskDecision(False, "score_below_threshold")
    return RiskDecision(True, "ok")
