"""Canonical regime/volatility feature primitives for TST Fusion research.

This module translates the retained Pine concepts into deterministic Python
features suitable for discovery and out-of-sample validation.  It deliberately
contains no entry rules and no live execution code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean
from typing import Sequence

from research.canonical_features import EPS, log_returns, population_variance


@dataclass(frozen=True)
class VarianceRatioResult:
    ratios: dict[int, float]
    composite: float
    trending_votes: int
    mean_reverting_votes: int
    regime: str


@dataclass(frozen=True)
class Garch11Result:
    omega: float
    alpha: float
    beta: float
    last_variance: float
    forecast_variance: float
    forecast_volatility: float
    objective: float


def _finite(values: Sequence[float]) -> list[float]:
    out = [float(v) for v in values]
    if any(not math.isfinite(v) for v in out):
        raise ValueError("series contains non-finite values")
    return out


def hurst_rs(prices: Sequence[float], sample_sizes: Sequence[int] = (32, 64, 128, 256, 512)) -> float:
    """Estimate the Hurst exponent with rescaled-range (R/S) analysis.

    Returns the slope of log(R/S) on log(window size).  Values around 0.5 are
    random-walk-like; values above/below 0.5 indicate persistence/anti-persistence.
    """
    r = log_returns(prices)
    points: list[tuple[float, float]] = []
    for n in sample_sizes:
        if n < 8 or n > len(r):
            continue
        rs_values: list[float] = []
        groups = len(r) // n
        for g in range(groups):
            chunk = r[g * n : (g + 1) * n]
            mu = fmean(chunk)
            dev = [x - mu for x in chunk]
            sd = math.sqrt(population_variance(chunk))
            if sd <= EPS:
                continue
            cumulative = []
            s = 0.0
            for x in dev:
                s += x
                cumulative.append(s)
            rng = max(cumulative) - min(cumulative)
            if rng > EPS:
                rs_values.append(rng / sd)
        if rs_values:
            points.append((math.log(n), math.log(fmean(rs_values))))
    if len(points) < 2:
        raise ValueError("not enough valid sample sizes for Hurst estimate")
    mx = fmean(x for x, _ in points)
    my = fmean(y for _, y in points)
    den = sum((x - mx) ** 2 for x, _ in points)
    if den <= EPS:
        raise ValueError("degenerate Hurst regression")
    return sum((x - mx) * (y - my) for x, y in points) / den


def variance_ratio(returns: Sequence[float], q: int) -> float:
    """Lo-MacKinlay-style variance ratio primitive without finite-sample z-test."""
    r = _finite(returns)
    if q < 2:
        raise ValueError("q must be >= 2")
    if len(r) < q * 3:
        raise ValueError("not enough returns for requested variance-ratio horizon")
    one = population_variance(r)
    if one <= EPS:
        return 1.0
    agg = [sum(r[i - q + 1 : i + 1]) for i in range(q - 1, len(r))]
    return population_variance(agg) / (q * one)


def variance_ratio_regime(
    prices: Sequence[float],
    horizons: Sequence[int] = (2, 4, 8, 16),
    trend_threshold: float = 1.05,
    mean_revert_threshold: float = 0.95,
    votes_required: int = 3,
) -> VarianceRatioResult:
    r = log_returns(prices)
    ratios = {int(q): variance_ratio(r, int(q)) for q in horizons}
    vals = list(ratios.values())
    trend_votes = sum(v > trend_threshold for v in vals)
    mr_votes = sum(v < mean_revert_threshold for v in vals)
    regime = "TREND" if trend_votes >= votes_required else "MEAN_REVERT" if mr_votes >= votes_required else "RANDOM"
    return VarianceRatioResult(ratios, fmean(vals), trend_votes, mr_votes, regime)


def shannon_entropy_returns(prices: Sequence[float], window: int = 64, bins: int = 8) -> float:
    """Normalized Shannon entropy of recent log-return states in [0, 1].

    This is intentionally computed from returns, not price levels, so it measures
    distributional uncertainty rather than the nominal price scale.
    """
    if window < 8 or bins < 2:
        raise ValueError("invalid entropy parameters")
    r = log_returns(prices)
    if len(r) < window:
        raise ValueError("not enough returns for entropy window")
    x = r[-window:]
    lo, hi = min(x), max(x)
    if hi - lo <= EPS:
        return 0.0
    counts = [0] * bins
    width = (hi - lo) / bins
    for value in x:
        idx = min(bins - 1, int((value - lo) / width))
        counts[idx] += 1
    entropy = 0.0
    for c in counts:
        if c:
            p = c / window
            entropy -= p * math.log(p)
    return entropy / math.log(bins)


def fractional_diff_weights(d: float, threshold: float = 1e-5, max_width: int = 1000) -> list[float]:
    """Fixed-width fractional-differentiation weights, newest observation first."""
    if not (0.0 < d < 1.0):
        raise ValueError("d must be between 0 and 1")
    if threshold <= 0 or max_width < 2:
        raise ValueError("invalid fractional-differentiation parameters")
    weights = [1.0]
    k = 1
    while k < max_width:
        w = -weights[-1] * (d - k + 1.0) / k
        if abs(w) < threshold:
            break
        weights.append(w)
        k += 1
    return weights


def fractional_difference(series: Sequence[float], d: float = 0.3, threshold: float = 1e-5, max_width: int = 1000) -> list[float | None]:
    x = _finite(series)
    w = fractional_diff_weights(d, threshold=threshold, max_width=max_width)
    width = len(w)
    out: list[float | None] = [None] * len(x)
    for i in range(width - 1, len(x)):
        out[i] = sum(w[k] * x[i - k] for k in range(width))
    return out


def _garch_filter(returns: Sequence[float], omega: float, alpha: float, beta: float) -> tuple[list[float], float]:
    r = _finite(returns)
    if not r:
        raise ValueError("returns cannot be empty")
    if omega <= 0 or alpha < 0 or beta < 0 or alpha + beta >= 1:
        raise ValueError("invalid stationary GARCH(1,1) parameters")
    unconditional = omega / max(EPS, 1.0 - alpha - beta)
    h = max(population_variance(r), unconditional, EPS)
    variances = [h]
    objective = 0.0
    for i in range(1, len(r)):
        h = omega + alpha * r[i - 1] ** 2 + beta * h
        h = max(h, EPS)
        variances.append(h)
        objective += math.log(h) + r[i] ** 2 / h  # Gaussian QMLE up to constants
    return variances, objective


def garch11_fit_forecast(prices: Sequence[float], horizon: int = 1) -> Garch11Result:
    """Dependency-free GARCH(1,1) QMLE grid fit and variance forecast.

    This intentionally replaces the retained Pine script's non-standard variance(1)
    recursion.  The grid is coarse by design: discovery uses the feature, while any
    production calibration can later swap in scipy/arch without changing semantics.
    """
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    r = log_returns(prices)
    if len(r) < 30:
        raise ValueError("at least 30 returns are required for GARCH fit")
    base_var = max(population_variance(r), EPS)
    best: tuple[float, float, float, float, list[float]] | None = None
    for alpha in (0.03, 0.05, 0.08, 0.12, 0.18):
        for beta in (0.70, 0.80, 0.88, 0.92, 0.95):
            if alpha + beta >= 0.995:
                continue
            omega = base_var * (1.0 - alpha - beta)
            if omega <= EPS:
                continue
            variances, obj = _garch_filter(r, omega, alpha, beta)
            if best is None or obj < best[0]:
                best = (obj, omega, alpha, beta, variances)
    if best is None:
        raise RuntimeError("GARCH calibration failed")
    obj, omega, alpha, beta, variances = best
    last_h = variances[-1]
    next_h = omega + alpha * r[-1] ** 2 + beta * last_h
    long_run = omega / (1.0 - alpha - beta)
    forecast = long_run + (alpha + beta) ** (horizon - 1) * (next_h - long_run)
    forecast = max(forecast, EPS)
    return Garch11Result(omega, alpha, beta, last_h, forecast, math.sqrt(forecast), obj)


REGIME_FEATURE_REGISTRY = {
    "hurst_rs": "KEEP: persistence / anti-persistence regime feature",
    "variance_ratio_regime": "KEEP: multi-horizon trend-vs-mean-reversion classifier",
    "shannon_entropy_returns": "KEEP: normalized market uncertainty feature",
    "fractional_difference": "KEEP: memory-preserving stationarity transform",
    "garch11_fit_forecast": "KEEP: conditional volatility forecast",
}
