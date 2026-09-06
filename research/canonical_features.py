"""Canonical quantitative feature primitives for TST Fusion research.

This module intentionally contains no trade-entry rules.  It converts raw market
series into deterministic, testable features so discovery/validation engines can
consume the same mathematics and avoid Pine/engine semantic drift.

All functions are pure-Python and dependency-free by design.  Live execution is
not enabled by this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean
from typing import Iterable, Sequence

EPS = 1e-12


@dataclass(frozen=True)
class CusumEvent:
    index: int
    direction: int  # +1 upward break, -1 downward break
    log_return: float
    threshold: float


@dataclass(frozen=True)
class BnsJumpResult:
    realized_variance: float
    bipower_variation: float
    tripower_quarticity: float
    jump_variance: float
    relative_jump: float
    z_stat: float
    regime: str


def _floats(values: Iterable[float]) -> list[float]:
    out = [float(v) for v in values]
    if any(not math.isfinite(v) for v in out):
        raise ValueError("series contains non-finite values")
    return out


def sample_variance(values: Sequence[float]) -> float:
    xs = _floats(values)
    if len(xs) < 2:
        return 0.0
    mu = fmean(xs)
    return sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)


def population_variance(values: Sequence[float]) -> float:
    xs = _floats(values)
    if not xs:
        return 0.0
    mu = fmean(xs)
    return sum((x - mu) ** 2 for x in xs) / len(xs)


def log_returns(prices: Sequence[float]) -> list[float]:
    p = _floats(prices)
    if any(x <= 0 for x in p):
        raise ValueError("prices must be positive")
    return [math.log(p[i] / p[i - 1]) for i in range(1, len(p))]


def realized_volatility(prices: Sequence[float], annualization: float = 365.0) -> float:
    """Annualized realized volatility from log returns.

    Crypto trades 365 days/year, so 365 is the canonical default rather than the
    252/253 equity convention used by several imported Pine scripts.
    """
    r = log_returns(prices)
    return math.sqrt(sample_variance(r)) * math.sqrt(annualization) if len(r) >= 2 else 0.0


def correlation(xs: Sequence[float], ys: Sequence[float]) -> float:
    x = _floats(xs)
    y = _floats(ys)
    if len(x) != len(y) or len(x) < 2:
        raise ValueError("series must have equal length >= 2")
    mx, my = fmean(x), fmean(y)
    dx = [v - mx for v in x]
    dy = [v - my for v in y]
    denom = math.sqrt(sum(v * v for v in dx) * sum(v * v for v in dy))
    return 0.0 if denom <= EPS else sum(a * b for a, b in zip(dx, dy)) / denom


def beta(asset_returns: Sequence[float], benchmark_returns: Sequence[float]) -> float:
    """Rolling-window beta primitive: Cov(asset, benchmark) / Var(benchmark)."""
    a = _floats(asset_returns)
    b = _floats(benchmark_returns)
    if len(a) != len(b) or len(a) < 2:
        raise ValueError("return series must have equal length >= 2")
    ma, mb = fmean(a), fmean(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (len(a) - 1)
    vb = sample_variance(b)
    return 0.0 if vb <= EPS else cov / vb


def rolling_beta(asset_prices: Sequence[float], benchmark_prices: Sequence[float], window: int = 60) -> list[float | None]:
    ar = log_returns(asset_prices)
    br = log_returns(benchmark_prices)
    if len(ar) != len(br):
        raise ValueError("asset and benchmark price series must align")
    if window < 2:
        raise ValueError("window must be >= 2")
    out: list[float | None] = [None] * len(ar)
    for i in range(window - 1, len(ar)):
        out[i] = beta(ar[i - window + 1 : i + 1], br[i - window + 1 : i + 1])
    return out


def rolling_correlation(asset_prices: Sequence[float], benchmark_prices: Sequence[float], window: int = 60) -> list[float | None]:
    ar = log_returns(asset_prices)
    br = log_returns(benchmark_prices)
    if len(ar) != len(br):
        raise ValueError("asset and benchmark price series must align")
    if window < 2:
        raise ValueError("window must be >= 2")
    out: list[float | None] = [None] * len(ar)
    for i in range(window - 1, len(ar)):
        out[i] = correlation(ar[i - window + 1 : i + 1], br[i - window + 1 : i + 1])
    return out


def skewness_excess_kurtosis(returns: Sequence[float]) -> tuple[float, float]:
    """Population skewness and excess kurtosis on returns, never price levels."""
    r = _floats(returns)
    if len(r) < 4:
        return 0.0, 0.0
    mu = fmean(r)
    m2 = fmean((x - mu) ** 2 for x in r)
    if m2 <= EPS:
        return 0.0, 0.0
    m3 = fmean((x - mu) ** 3 for x in r)
    m4 = fmean((x - mu) ** 4 for x in r)
    sigma = math.sqrt(m2)
    return m3 / sigma**3, m4 / sigma**4 - 3.0


def amihud_illiquidity(closes: Sequence[float], quote_volumes: Sequence[float], window: int = 20) -> list[float | None]:
    """Amihud |return| / quote-volume using quote/dollar volume for comparability."""
    c = _floats(closes)
    qv = _floats(quote_volumes)
    if len(c) != len(qv):
        raise ValueError("closes and quote_volumes must align")
    if window < 1:
        raise ValueError("window must be >= 1")
    raw: list[float] = []
    for i in range(1, len(c)):
        ret = abs(c[i] / c[i - 1] - 1.0)
        raw.append(ret / qv[i] if qv[i] > EPS else 0.0)
    out: list[float | None] = [None] * len(raw)
    for i in range(window - 1, len(raw)):
        out[i] = fmean(raw[i - window + 1 : i + 1])
    return out


def cusum_vol_adaptive(
    prices: Sequence[float],
    vol_lookback: int = 20,
    threshold_mult: float = 1.0,
    min_bar_gap: int = 5,
) -> list[CusumEvent]:
    """Symmetric CUSUM event filter with a volatility-adaptive threshold.

    The threshold is computed from trailing sample standard deviation of log
    returns. This is an event sampler / structural-break feature, not an entry
    signal. The production research pipeline may swap the trailing stdev for EWM
    volatility while keeping this interface stable.
    """
    if vol_lookback < 2 or threshold_mult <= 0 or min_bar_gap < 1:
        raise ValueError("invalid CUSUM parameters")
    r = log_returns(prices)
    s_pos = 0.0
    s_neg = 0.0
    last_event = -10**9
    events: list[CusumEvent] = []
    for i, ret in enumerate(r):
        if i < vol_lookback - 1:
            continue
        sigma = math.sqrt(sample_variance(r[i - vol_lookback + 1 : i + 1]))
        h = threshold_mult * sigma
        if h <= EPS:
            continue
        s_pos = max(0.0, s_pos + ret)
        s_neg = min(0.0, s_neg + ret)
        can_fire = (i - last_event) >= min_bar_gap
        if can_fire and s_pos >= h:
            events.append(CusumEvent(i + 1, 1, ret, h))
            s_pos = s_neg = 0.0
            last_event = i
        elif can_fire and s_neg <= -h:
            events.append(CusumEvent(i + 1, -1, ret, h))
            s_pos = s_neg = 0.0
            last_event = i
    return events


def bns_jump_statistic(prices: Sequence[float], window: int = 22, regime_lo: float = 0.20, regime_hi: float = 0.50) -> BnsJumpResult:
    """BNS realized-variance decomposition on the latest window.

    Implements RV, bipower variation, tripower quarticity and the ratio-form
    BNS statistic used by the retained TradingView reference. It is intended as
    a jump/tail-risk feature; significance thresholds should be validated OOS.
    """
    if window < 5:
        raise ValueError("window must be >= 5")
    r_all = log_returns(prices)
    if len(r_all) < window + 2:
        raise ValueError("not enough observations for BNS window")
    r = r_all[-window:]
    rv = sum(x * x for x in r)
    bv = (math.pi / 2.0) * sum(abs(r[i]) * abs(r[i - 1]) for i in range(1, len(r)))
    mu43_inv3 = 1.7425
    tq_sum = 0.0
    for i in range(2, len(r)):
        tq_sum += abs(r[i]) ** (4.0 / 3.0) * abs(r[i - 1]) ** (4.0 / 3.0) * abs(r[i - 2]) ** (4.0 / 3.0)
    tq = window * mu43_inv3 * tq_sum
    jump = max(0.0, rv - bv)
    rj_raw = (rv - bv) / rv if rv > EPS else 0.0
    rj = jump / rv if rv > EPS else 0.0
    theta = math.pi**2 / 4.0 + math.pi - 5.0
    infl = max(1.0, tq / (bv * bv)) if bv > EPS else 1.0
    z = math.sqrt(window) * rj_raw / math.sqrt(theta * infl) if bv > EPS else 0.0
    regime = "JUMP" if rj >= regime_hi else "CONTINUOUS" if rj <= regime_lo else "MIXED"
    return BnsJumpResult(rv, bv, tq, jump, rj, z, regime)


FEATURE_REGISTRY = {
    "realized_volatility": "KEEP: multi-horizon volatility / regime input",
    "rolling_beta_btc": "KEEP: portfolio BTC sensitivity",
    "rolling_correlation_btc": "KEEP: concentration / duplicate-risk control",
    "skewness_excess_kurtosis": "KEEP: tail-shape feature on returns",
    "amihud_illiquidity": "KEEP: liquidity stress using quote volume",
    "cusum_vol_adaptive": "KEEP: structural-break event sampler",
    "bns_jump_statistic": "KEEP: statistically grounded jump decomposition",
}
