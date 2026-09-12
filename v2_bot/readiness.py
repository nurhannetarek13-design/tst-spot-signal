from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


DEFAULT_MIN_SHADOW_DECISIVE = 60
DEFAULT_MIN_SHADOW_PROFIT_FACTOR = 1.20
DEFAULT_MIN_SHADOW_EXPECTANCY_USDT = 0.0
DEFAULT_MAX_SHADOW_AMBIGUOUS_RATE = 0.10

DEFAULT_MIN_PAPER_CLOSED = 60
DEFAULT_MIN_PAPER_PROFIT_FACTOR = 1.20
DEFAULT_MIN_PAPER_EXPECTANCY_USDT = 0.0


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"ready": self.ready, "blockers": list(self.blockers)}


def _report(blockers: Iterable[str]) -> ReadinessReport:
    unique = tuple(dict.fromkeys(str(item) for item in blockers if item))
    return ReadinessReport(ready=not unique, blockers=unique)


def evaluate_strategy_evidence(
    stats: Mapping[str, object],
    *,
    min_decisive: int = DEFAULT_MIN_SHADOW_DECISIVE,
    min_profit_factor: float = DEFAULT_MIN_SHADOW_PROFIT_FACTOR,
    min_expectancy_usdt: float = DEFAULT_MIN_SHADOW_EXPECTANCY_USDT,
    max_ambiguous_rate: float = DEFAULT_MAX_SHADOW_AMBIGUOUS_RATE,
) -> ReadinessReport:
    """Conservative SHADOW-evidence gate for future real-money promotion."""

    blockers: list[str] = []
    if min_decisive <= 0 or min_profit_factor <= 0 or min_expectancy_usdt < 0:
        return _report(["invalid_strategy_evidence_thresholds"])
    if not 0 <= max_ambiguous_rate <= 1:
        return _report(["invalid_strategy_evidence_thresholds"])

    try:
        decisive = int(stats.get("decisive", 0) or 0)
    except (TypeError, ValueError):
        decisive = -1
    if decisive < min_decisive:
        blockers.append("shadow_sample_insufficient")

    profit_factor_raw = stats.get("profit_factor")
    try:
        profit_factor = float(profit_factor_raw) if profit_factor_raw is not None else None
    except (TypeError, ValueError):
        profit_factor = None
    if profit_factor is None or profit_factor < min_profit_factor:
        blockers.append("shadow_profit_factor_unproven")

    expectancy_raw = stats.get("expectancy_usdt")
    try:
        expectancy = float(expectancy_raw) if expectancy_raw is not None else None
    except (TypeError, ValueError):
        expectancy = None
    if expectancy is None or expectancy <= min_expectancy_usdt:
        blockers.append("shadow_expectancy_nonpositive")

    ambiguity_raw = stats.get("ambiguous_rate")
    try:
        ambiguity = float(ambiguity_raw) if ambiguity_raw is not None else None
    except (TypeError, ValueError):
        ambiguity = None
    if ambiguity is None or ambiguity > max_ambiguous_rate:
        blockers.append("shadow_ambiguity_too_high")

    return _report(blockers)


def evaluate_paper_evidence(
    stats: Mapping[str, object],
    *,
    min_closed: int = DEFAULT_MIN_PAPER_CLOSED,
    min_profit_factor: float = DEFAULT_MIN_PAPER_PROFIT_FACTOR,
    min_expectancy_usdt: float = DEFAULT_MIN_PAPER_EXPECTANCY_USDT,
) -> ReadinessReport:
    """Fee-aware PAPER evidence gate for future real-money promotion.

    Paper is still simulated execution, so passing this gate is necessary
    evidence rather than a guarantee of future profitability.
    """

    blockers: list[str] = []
    if min_closed <= 0 or min_profit_factor <= 0 or min_expectancy_usdt < 0:
        return _report(["invalid_paper_evidence_thresholds"])

    try:
        closed = int(stats.get("closed", 0) or 0)
    except (TypeError, ValueError):
        closed = -1
    if closed < min_closed:
        blockers.append("paper_sample_insufficient")

    profit_factor_raw = stats.get("profit_factor")
    try:
        profit_factor = float(profit_factor_raw) if profit_factor_raw is not None else None
    except (TypeError, ValueError):
        profit_factor = None
    if profit_factor is None or profit_factor < min_profit_factor:
        blockers.append("paper_profit_factor_unproven")

    expectancy_raw = stats.get("expectancy_usdt")
    try:
        expectancy = float(expectancy_raw) if expectancy_raw is not None else None
    except (TypeError, ValueError):
        expectancy = None
    if expectancy is None or expectancy <= min_expectancy_usdt:
        blockers.append("paper_expectancy_nonpositive")

    return _report(blockers)


def evaluate_paper_readiness(
    *,
    persistent_state_enabled: bool,
    persistence_proven: bool,
    deploy_revision_present: bool,
    exchange_preflight_available: bool,
) -> ReadinessReport:
    blockers: list[str] = []
    if not persistent_state_enabled:
        blockers.append("persistent_state_disabled")
    if not persistence_proven:
        blockers.append("persistent_storage_not_proven")
    if not deploy_revision_present:
        blockers.append("deploy_revision_missing")
    if not exchange_preflight_available:
        blockers.append("exchange_preflight_unavailable")
    return _report(blockers)


def evaluate_live_readiness(
    *,
    persistent_state_enabled: bool,
    persistence_proven: bool,
    deploy_revision_present: bool,
    exchange_preflight_available: bool,
    pending_execution_count: int,
    api_credentials_present: bool,
    private_adapter_wired: bool,
    explicit_live_authorization: bool,
    live_engine_lock_removed: bool,
    emergency_flatten_verified: bool,
    strategy_evidence_ready: bool = False,
    paper_evidence_ready: bool = False,
) -> ReadinessReport:
    blockers = list(
        evaluate_paper_readiness(
            persistent_state_enabled=persistent_state_enabled,
            persistence_proven=persistence_proven,
            deploy_revision_present=deploy_revision_present,
            exchange_preflight_available=exchange_preflight_available,
        ).blockers
    )

    if not (strategy_evidence_ready or paper_evidence_ready):
        blockers.append("strategy_evidence_not_proven")
    if pending_execution_count < 0:
        blockers.append("invalid_pending_execution_count")
    elif pending_execution_count:
        blockers.append("pending_execution_recovery_required")
    if not api_credentials_present:
        blockers.append("private_api_credentials_missing")
    if not private_adapter_wired:
        blockers.append("private_adapter_not_wired")
    if not explicit_live_authorization:
        blockers.append("live_not_authorized")
    if not live_engine_lock_removed:
        blockers.append("live_engine_hard_lock_active")
    if not emergency_flatten_verified:
        blockers.append("emergency_flatten_not_verified")

    return _report(blockers)
