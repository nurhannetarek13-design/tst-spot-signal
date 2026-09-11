from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"ready": self.ready, "blockers": list(self.blockers)}


def _report(blockers: Iterable[str]) -> ReadinessReport:
    unique = tuple(dict.fromkeys(str(item) for item in blockers if item))
    return ReadinessReport(ready=not unique, blockers=unique)


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
) -> ReadinessReport:
    blockers = list(
        evaluate_paper_readiness(
            persistent_state_enabled=persistent_state_enabled,
            persistence_proven=persistence_proven,
            deploy_revision_present=deploy_revision_present,
            exchange_preflight_available=exchange_preflight_available,
        ).blockers
    )

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
