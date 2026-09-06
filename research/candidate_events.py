"""Canonical candidate-event definitions for TST Fusion discovery.

This layer consumes already-computed, time-aligned features and emits event masks.
It contains no execution logic, sizing, or future-looking filters. Candidate
thresholds are explicit and must be frozen before out-of-sample validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class CandidateDefinition:
    name: str
    direction: int
    required_features: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class CandidateEvents:
    definition: CandidateDefinition
    indices: tuple[int, ...]


def _aligned(features: Mapping[str, Sequence[float]], required: Sequence[str]) -> int:
    missing = [name for name in required if name not in features]
    if missing:
        raise ValueError(f"missing required features: {missing}")
    lengths = {len(features[name]) for name in required}
    if len(lengths) != 1:
        raise ValueError("candidate feature series must align")
    n = next(iter(lengths))
    if n == 0:
        raise ValueError("candidate features cannot be empty")
    return n


def _indices(mask: Sequence[bool]) -> tuple[int, ...]:
    return tuple(i for i, flag in enumerate(mask) if flag)


def liquidity_crash_exhaustion(
    features: Mapping[str, Sequence[float]], *,
    residual_z_max: float = -2.5,
    jump_rj_min: float = 0.20,
    amihud_z_min: float = 1.5,
    sell_imbalance_max: float = -0.20,
    recovery_flow_min: float = -0.05,
) -> CandidateEvents:
    """Long exhaustion event after a statistically abnormal liquidity crash.

    Required inputs are contemporaneous features only. `aggressor_imbalance_delta`
    should represent the change versus the previous bar, so a positive value means
    selling pressure is easing rather than using future confirmation.
    """
    required = (
        "residual_z", "bns_relative_jump", "amihud_z",
        "aggressor_imbalance", "aggressor_imbalance_delta",
    )
    n = _aligned(features, required)
    mask = []
    for i in range(n):
        mask.append(
            features["residual_z"][i] <= residual_z_max
            and features["bns_relative_jump"][i] >= jump_rj_min
            and features["amihud_z"][i] >= amihud_z_min
            and features["aggressor_imbalance"][i] <= sell_imbalance_max
            and features["aggressor_imbalance_delta"][i] >= recovery_flow_min
        )
    definition = CandidateDefinition(
        "LIQUIDITY_CRASH_EXHAUSTION_V1", 1, required,
        "Extreme negative residual move + jump/liquidity stress + easing sell aggression.",
    )
    return CandidateEvents(definition, _indices(mask))


def residual_dispersion_reversal(
    features: Mapping[str, Sequence[float]], *,
    residual_z_max: float = -3.0,
    entropy_max: float = 0.85,
    hurst_max: float = 0.55,
    flow_reversal_min: float = 0.05,
) -> CandidateEvents:
    """Long reversal after extreme residual underperformance with exhaustion."""
    required = ("residual_z", "entropy", "hurst", "aggressor_imbalance_delta")
    n = _aligned(features, required)
    mask = []
    for i in range(n):
        mask.append(
            features["residual_z"][i] <= residual_z_max
            and features["entropy"][i] <= entropy_max
            and features["hurst"][i] <= hurst_max
            and features["aggressor_imbalance_delta"][i] >= flow_reversal_min
        )
    definition = CandidateDefinition(
        "EXTREME_RESIDUAL_DISPERSION_REVERSAL_V1", 1, required,
        "Residual z <= -3 with non-random regime and contemporaneous flow reversal.",
    )
    return CandidateEvents(definition, _indices(mask))


def breadth_lead_lag_continuation(
    features: Mapping[str, Sequence[float]], *,
    breadth_min: float = 0.65,
    btc_lead_return_min: float = 0.01,
    asset_relative_strength_max: float = 0.0,
    buy_share_min: float = 0.56,
    oi_change_pct_min: float = 0.0,
) -> CandidateEvents:
    """Long continuation event when broad market strength leads a lagging asset."""
    required = (
        "breadth_positive_share", "btc_lead_return", "relative_strength_btc",
        "taker_buy_share", "oi_change_pct",
    )
    n = _aligned(features, required)
    mask = []
    for i in range(n):
        mask.append(
            features["breadth_positive_share"][i] >= breadth_min
            and features["btc_lead_return"][i] >= btc_lead_return_min
            and features["relative_strength_btc"][i] <= asset_relative_strength_max
            and features["taker_buy_share"][i] >= buy_share_min
            and features["oi_change_pct"][i] >= oi_change_pct_min
        )
    definition = CandidateDefinition(
        "BREADTH_LEAD_LAG_CONTINUATION_V1", 1, required,
        "Positive market breadth + BTC lead + lagging asset + confirming taker/OI flow.",
    )
    return CandidateEvents(definition, _indices(mask))


def canonical_candidate_set(features: Mapping[str, Sequence[float]]) -> tuple[CandidateEvents, ...]:
    """Build the three priority Round-3 candidate families with frozen V1 defaults."""
    return (
        liquidity_crash_exhaustion(features),
        residual_dispersion_reversal(features),
        breadth_lead_lag_continuation(features),
    )
