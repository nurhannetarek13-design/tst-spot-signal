"""Round-3 raw edge evaluation for canonical TST candidates.

The runner evaluates event definitions against unoptimized forward returns. It does
not place trades, size positions, or optimize thresholds. Threshold changes create
a new candidate definition/version and must be re-evaluated from scratch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from research.candidate_events import CandidateEvents, canonical_candidate_set
from research.discovery_pipeline import CandidateEvidence, evaluate_candidate


@dataclass(frozen=True)
class Round3Report:
    candidates: tuple[CandidateEvidence, ...]

    @property
    def passing(self) -> tuple[CandidateEvidence, ...]:
        return tuple(c for c in self.candidates if c.all_required_pass)


def evaluate_round3(
    features: Mapping[str, Sequence[float]],
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    *,
    horizons: Sequence[int] = (24, 48, 72),
    min_gap: int = 12,
    min_events: int = 30,
    require_all_horizons: bool = False,
) -> Round3Report:
    """Evaluate frozen V1 candidate events against raw conditional returns."""
    events = canonical_candidate_set(features)
    evidence: list[CandidateEvidence] = []
    for candidate in events:
        evidence.append(
            evaluate_candidate(
                candidate.definition.name,
                closes,
                highs,
                lows,
                candidate.indices,
                horizons,
                direction=candidate.definition.direction,
                min_gap=min_gap,
                min_events=min_events,
                require_all_horizons=require_all_horizons,
            )
        )
    return Round3Report(tuple(evidence))


def rank_candidates(report: Round3Report) -> tuple[CandidateEvidence, ...]:
    """Rank evidence without changing pass/fail gates.

    Ordering uses the best passed-horizon mean return, then event count. A candidate
    that fails every gate remains a failed candidate regardless of rank.
    """
    def score(c: CandidateEvidence) -> tuple[float, int]:
        best = max((g.mean_return for g in c.gates.values()), default=float("-inf"))
        return best, c.events

    return tuple(sorted(report.candidates, key=score, reverse=True))
