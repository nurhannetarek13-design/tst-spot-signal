"""Out-of-sample, walk-forward, and Monte Carlo validation primitives.

Research-only. These helpers validate already-frozen candidate event definitions;
they do not optimize thresholds and cannot authorize live trading.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import fmean, median
from typing import Sequence


@dataclass(frozen=True)
class WalkForwardFold:
    train_start: int
    train_end: int
    test_start: int
    test_end: int


@dataclass(frozen=True)
class MonteCarloSummary:
    simulations: int
    median_final_equity: float
    p05_final_equity: float
    p95_final_equity: float
    median_max_drawdown: float
    p95_max_drawdown: float
    ruin_probability: float


def walk_forward_folds(
    n: int,
    train_size: int,
    test_size: int,
    *,
    step_size: int | None = None,
    purge: int = 0,
    embargo: int = 0,
    anchored: bool = False,
) -> list[WalkForwardFold]:
    """Create deterministic rolling or anchored walk-forward folds.

    purge removes observations immediately before the test set from training;
    embargo skips observations after each test set before the next fold starts.
    """
    if n < 1 or train_size < 2 or test_size < 1 or purge < 0 or embargo < 0:
        raise ValueError("invalid walk-forward parameters")
    step = test_size if step_size is None else int(step_size)
    if step < 1:
        raise ValueError("step_size must be >= 1")
    folds: list[WalkForwardFold] = []
    test_start = train_size + purge
    while test_start + test_size <= n:
        train_end = test_start - purge
        train_start = 0 if anchored else max(0, train_end - train_size)
        if train_end - train_start >= 2:
            folds.append(WalkForwardFold(train_start, train_end, test_start, test_start + test_size))
        test_start += step + embargo
    return folds


def apply_embargo(indices: Sequence[int], test_start: int, test_end: int, embargo: int) -> list[int]:
    """Drop indices overlapping a test span or its post-test embargo."""
    if test_start < 0 or test_end <= test_start or embargo < 0:
        raise ValueError("invalid embargo range")
    lo, hi = test_start, test_end + embargo
    return [int(i) for i in indices if not (lo <= int(i) < hi)]


def _quantile(xs: Sequence[float], q: float) -> float:
    if not xs:
        raise ValueError("empty series")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0,1]")
    s = sorted(float(x) for x in xs)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return s[lo]
    w = pos - lo
    return s[lo] * (1 - w) + s[hi] * w


def equity_curve_from_returns(returns: Sequence[float], starting_equity: float = 1.0) -> list[float]:
    if starting_equity <= 0:
        raise ValueError("starting_equity must be positive")
    eq = float(starting_equity)
    out: list[float] = []
    for r in returns:
        rr = float(r)
        if rr <= -1.0 or not math.isfinite(rr):
            raise ValueError("returns must be finite and > -100%")
        eq *= 1.0 + rr
        out.append(eq)
    return out


def max_drawdown_fraction(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = float(equity[0])
    worst = 0.0
    for x in equity:
        v = float(x)
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def monte_carlo_trade_paths(
    returns: Sequence[float],
    *,
    simulations: int = 5000,
    starting_equity: float = 1.0,
    seed: int = 7,
    ruin_drawdown: float = 0.30,
) -> MonteCarloSummary:
    """Bootstrap closed-trade returns with replacement and summarize path risk."""
    r = [float(x) for x in returns]
    if len(r) < 2 or simulations < 100 or starting_equity <= 0:
        raise ValueError("insufficient Monte Carlo inputs")
    if any((not math.isfinite(x)) or x <= -1.0 for x in r):
        raise ValueError("returns must be finite and > -100%")
    if not 0 < ruin_drawdown < 1:
        raise ValueError("ruin_drawdown must be in (0,1)")
    rng = random.Random(seed)
    finals: list[float] = []
    dds: list[float] = []
    ruined = 0
    for _ in range(simulations):
        sample = [r[rng.randrange(len(r))] for _ in range(len(r))]
        eq = equity_curve_from_returns(sample, starting_equity)
        dd = max_drawdown_fraction(eq)
        finals.append(eq[-1])
        dds.append(dd)
        ruined += int(dd >= ruin_drawdown)
    return MonteCarloSummary(
        simulations=simulations,
        median_final_equity=median(finals),
        p05_final_equity=_quantile(finals, 0.05),
        p95_final_equity=_quantile(finals, 0.95),
        median_max_drawdown=median(dds),
        p95_max_drawdown=_quantile(dds, 0.95),
        ruin_probability=ruined / simulations,
    )


def oos_degradation(in_sample_metric: float, out_of_sample_metric: float) -> float:
    """Relative OOS degradation; positive means worse out of sample."""
    ins, oos = float(in_sample_metric), float(out_of_sample_metric)
    if not (math.isfinite(ins) and math.isfinite(oos)) or abs(ins) < 1e-12:
        raise ValueError("metrics must be finite and in-sample metric non-zero")
    return (ins - oos) / abs(ins)


def aggregate_fold_metric(values: Sequence[float]) -> dict[str, float]:
    xs = [float(x) for x in values]
    if not xs or any(not math.isfinite(x) for x in xs):
        raise ValueError("finite fold metrics required")
    return {"mean": fmean(xs), "median": median(xs), "worst": min(xs), "best": max(xs)}
