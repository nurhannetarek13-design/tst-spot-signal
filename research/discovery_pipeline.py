"""Canonical event-discovery and evidence-gate primitives for TST Fusion.

This module converts feature/event masks into raw conditional forward-return
statistics. It deliberately avoids strategy entries, sizing, and live execution.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean, median
from typing import Mapping, Sequence

EPS = 1e-12


@dataclass(frozen=True)
class ForwardGateResult:
    horizon: int
    n: int
    mean_return: float
    median_return: float
    hit_rate: float
    median_mfe: float
    median_mae: float
    mfe_mae_ratio: float
    passed: bool


@dataclass(frozen=True)
class CandidateEvidence:
    name: str
    events: int
    gates: Mapping[int, ForwardGateResult]
    passed_horizons: tuple[int, ...]
    all_required_pass: bool


def _finite(xs: Sequence[float]) -> list[float]:
    out = [float(x) for x in xs]
    if any(not math.isfinite(x) for x in out):
        raise ValueError("series contains non-finite values")
    return out


def event_indices(mask: Sequence[bool]) -> list[int]:
    return [i for i, flag in enumerate(mask) if bool(flag)]


def decluster_events(indices: Sequence[int], min_gap: int = 1) -> list[int]:
    if min_gap < 1:
        raise ValueError("min_gap must be >= 1")
    out: list[int] = []
    last = -10**18
    for i in sorted(int(x) for x in indices):
        if i - last >= min_gap:
            out.append(i)
            last = i
    return out


def conditional_forward_stats(
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    indices: Sequence[int],
    horizon: int,
    direction: int = 1,
) -> ForwardGateResult:
    c, h, l = _finite(closes), _finite(highs), _finite(lows)
    if not (len(c) == len(h) == len(l)):
        raise ValueError("OHLC series must align")
    if horizon < 1 or direction not in (-1, 1):
        raise ValueError("invalid horizon or direction")
    rets: list[float] = []
    mfes: list[float] = []
    maes: list[float] = []
    for idx in indices:
        i = int(idx)
        if i < 0 or i + horizon >= len(c):
            continue
        entry = c[i]
        exit_px = c[i + horizon]
        ret = direction * (exit_px / entry - 1.0)
        future_h = max(h[i + 1 : i + horizon + 1])
        future_l = min(l[i + 1 : i + horizon + 1])
        if direction == 1:
            mfe = future_h / entry - 1.0
            mae = max(0.0, 1.0 - future_l / entry)
        else:
            mfe = entry / future_l - 1.0
            mae = max(0.0, future_h / entry - 1.0)
        rets.append(ret)
        mfes.append(max(0.0, mfe))
        maes.append(max(0.0, mae))
    if not rets:
        return ForwardGateResult(horizon, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, False)
    med_mfe = median(mfes)
    med_mae = median(maes)
    ratio = med_mfe / med_mae if med_mae > EPS else (float("inf") if med_mfe > 0 else 0.0)
    mean_ret = fmean(rets)
    med_ret = median(rets)
    hit = sum(r > 0 for r in rets) / len(rets)
    passed = mean_ret >= 0.015 and med_ret > 0 and hit > 0.55 and ratio >= 2.0
    return ForwardGateResult(horizon, len(rets), mean_ret, med_ret, hit, med_mfe, med_mae, ratio, passed)


def evaluate_candidate(
    name: str,
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    indices: Sequence[int],
    horizons: Sequence[int] = (24, 48, 72),
    *,
    direction: int = 1,
    min_gap: int = 1,
    min_events: int = 30,
    require_all_horizons: bool = False,
) -> CandidateEvidence:
    idx = decluster_events(indices, min_gap=min_gap)
    gates = {int(h): conditional_forward_stats(closes, highs, lows, idx, int(h), direction) for h in horizons}
    passed = tuple(h for h, g in gates.items() if g.n >= min_events and g.passed)
    if require_all_horizons:
        all_pass = len(passed) == len(gates) and len(gates) > 0
    else:
        all_pass = len(passed) > 0
    return CandidateEvidence(name, len(idx), gates, passed, all_pass)


def feature_redundancy_matrix(features: Mapping[str, Sequence[float]]) -> dict[tuple[str, str], float]:
    """Pairwise Pearson correlation for aligned feature vectors.

    Used only to flag redundancy; it is not a selection rule by itself.
    """
    names = list(features)
    arrays = {k: _finite(features[k]) for k in names}
    if not names:
        return {}
    n = len(arrays[names[0]])
    if n < 2 or any(len(v) != n for v in arrays.values()):
        raise ValueError("all feature series must align and contain >=2 points")
    out: dict[tuple[str, str], float] = {}
    for i, a_name in enumerate(names):
        a = arrays[a_name]
        ma = fmean(a)
        da = [x - ma for x in a]
        saa = sum(x*x for x in da)
        for b_name in names[i:]:
            b = arrays[b_name]
            mb = fmean(b)
            db = [x - mb for x in b]
            sbb = sum(x*x for x in db)
            den = math.sqrt(saa * sbb)
            corr = 0.0 if den <= EPS else sum(x*y for x, y in zip(da, db)) / den
            out[(a_name, b_name)] = corr
            out[(b_name, a_name)] = corr
    return out


def redundant_pairs(features: Mapping[str, Sequence[float]], threshold: float = 0.90) -> list[tuple[str, str, float]]:
    if not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0,1]")
    matrix = feature_redundancy_matrix(features)
    names = list(features)
    pairs: list[tuple[str, str, float]] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            corr = matrix[(a, b)]
            if abs(corr) >= threshold:
                pairs.append((a, b, corr))
    return pairs
