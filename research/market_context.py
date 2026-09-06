"""Canonical market-context and labeling primitives for TST Fusion research.

No live execution. Inputs are explicit market series so every validator can consume
identical semantics independent of TradingView/Hummingbot/Freqtrade implementations.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean
from typing import Mapping, Sequence

from research.canonical_features import EPS, log_returns


@dataclass(frozen=True)
class OrderFlowResult:
    delta: float
    imbalance: float
    buy_share: float


@dataclass(frozen=True)
class DerivativesContext:
    oi_change: float
    oi_change_pct: float
    basis_pct: float
    spot_perp_return_spread: float


@dataclass(frozen=True)
class TripleBarrierLabel:
    entry_index: int
    exit_index: int
    direction: int
    outcome: int  # +1 PT, -1 SL, 0 time barrier
    return_pct: float
    mfe_pct: float
    mae_pct: float


def _finite(xs: Sequence[float]) -> list[float]:
    out = [float(x) for x in xs]
    if any(not math.isfinite(x) for x in out):
        raise ValueError("series contains non-finite values")
    return out


def order_flow_from_aggressor_volume(buy_volume: Sequence[float], sell_volume: Sequence[float], window: int = 1) -> OrderFlowResult:
    """Aggregate true/externally-supplied aggressor buy/sell volume.

    This intentionally avoids candle-body volume proxies when exchange-side taker
    volume is available.
    """
    b, s = _finite(buy_volume), _finite(sell_volume)
    if len(b) != len(s) or not b or window < 1:
        raise ValueError("aligned non-empty buy/sell volume required")
    bsum = sum(b[-window:])
    ssum = sum(s[-window:])
    total = bsum + ssum
    delta = bsum - ssum
    return OrderFlowResult(delta, delta / total if total > EPS else 0.0, bsum / total if total > EPS else 0.5)


def open_interest_delta(open_interest: Sequence[float]) -> tuple[float, float]:
    oi = _finite(open_interest)
    if len(oi) < 2:
        raise ValueError("need at least two OI observations")
    change = oi[-1] - oi[-2]
    pct = change / oi[-2] if abs(oi[-2]) > EPS else 0.0
    return change, pct


def spot_perp_context(spot_prices: Sequence[float], perp_prices: Sequence[float], open_interest: Sequence[float]) -> DerivativesContext:
    s, p = _finite(spot_prices), _finite(perp_prices)
    if len(s) != len(p) or len(s) < 2:
        raise ValueError("spot/perp prices must align and contain >=2 points")
    oi_change, oi_pct = open_interest_delta(open_interest)
    basis = (p[-1] / s[-1] - 1.0) if s[-1] > EPS else 0.0
    sr = math.log(s[-1] / s[-2])
    pr = math.log(p[-1] / p[-2])
    return DerivativesContext(oi_change, oi_pct, basis, sr - pr)


def cvd_from_delta(deltas: Sequence[float]) -> list[float]:
    ds = _finite(deltas)
    out, acc = [], 0.0
    for d in ds:
        acc += d
        out.append(acc)
    return out


def spot_perp_cvd_divergence(spot_delta: Sequence[float], perp_delta: Sequence[float], window: int = 20) -> float:
    sd, pd = _finite(spot_delta), _finite(perp_delta)
    if len(sd) != len(pd) or len(sd) < window or window < 2:
        raise ValueError("aligned delta series with enough history required")
    sc = sum(sd[-window:])
    pc = sum(pd[-window:])
    scale = max(sum(abs(x) for x in sd[-window:]), sum(abs(x) for x in pd[-window:]), EPS)
    return (sc - pc) / scale


def relative_strength(asset_prices: Sequence[float], benchmark_prices: Sequence[float], lookback: int = 60) -> float:
    a, b = _finite(asset_prices), _finite(benchmark_prices)
    if len(a) != len(b) or len(a) <= lookback or lookback < 1:
        raise ValueError("aligned price series longer than lookback required")
    ar = a[-1] / a[-1 - lookback] - 1.0
    br = b[-1] / b[-1 - lookback] - 1.0
    return ar - br


def triple_barrier_label(
    closes: Sequence[float], highs: Sequence[float], lows: Sequence[float], *,
    entry_index: int, direction: int, pt_pct: float, sl_pct: float, max_hold: int,
) -> TripleBarrierLabel:
    """Conservative triple-barrier label; same-bar PT/SL ties resolve stop-first."""
    c, h, l = _finite(closes), _finite(highs), _finite(lows)
    if not (len(c) == len(h) == len(l)):
        raise ValueError("OHLC series must align")
    if direction not in (-1, 1) or pt_pct <= 0 or sl_pct <= 0 or max_hold < 1:
        raise ValueError("invalid barrier parameters")
    if entry_index < 0 or entry_index >= len(c) - 1:
        raise ValueError("entry_index must have future bars")
    entry = c[entry_index]
    pt = entry * (1 + direction * pt_pct)
    sl = entry * (1 - direction * sl_pct)
    last = min(len(c) - 1, entry_index + max_hold)
    mfe = 0.0
    mae = 0.0
    outcome = 0
    exit_i = last
    for i in range(entry_index + 1, last + 1):
        fav = (h[i] / entry - 1.0) if direction == 1 else (entry / l[i] - 1.0)
        adv = (1.0 - l[i] / entry) if direction == 1 else (h[i] / entry - 1.0)
        mfe = max(mfe, fav)
        mae = max(mae, adv)
        sl_hit = l[i] <= sl if direction == 1 else h[i] >= sl
        pt_hit = h[i] >= pt if direction == 1 else l[i] <= pt
        if sl_hit:
            outcome, exit_i = -1, i
            break
        if pt_hit:
            outcome, exit_i = 1, i
            break
    ret = direction * (c[exit_i] / entry - 1.0)
    if outcome == 1:
        ret = pt_pct
    elif outcome == -1:
        ret = -sl_pct
    return TripleBarrierLabel(entry_index, exit_i, direction, outcome, ret, mfe, mae)


def forward_returns(prices: Sequence[float], horizons: Sequence[int]) -> dict[int, list[float | None]]:
    p = _finite(prices)
    out: dict[int, list[float | None]] = {}
    for h in horizons:
        if h < 1:
            raise ValueError("horizons must be positive")
        vals: list[float | None] = [None] * len(p)
        for i in range(len(p) - h):
            vals[i] = p[i + h] / p[i] - 1.0
        out[int(h)] = vals
    return out


def ablation_delta(baseline_metric: float, candidate_metric: float) -> float:
    """Simple canonical attribution primitive: positive means candidate improves metric."""
    if not (math.isfinite(baseline_metric) and math.isfinite(candidate_metric)):
        raise ValueError("metrics must be finite")
    return candidate_metric - baseline_metric


MARKET_CONTEXT_REGISTRY: Mapping[str, str] = {
    "order_flow_from_aggressor_volume": "KEEP: taker/aggressor imbalance",
    "spot_perp_context": "KEEP: basis/OI/spot-perp divergence",
    "spot_perp_cvd_divergence": "KEEP: flow divergence",
    "relative_strength": "KEEP: asset-vs-BTC relative momentum",
    "triple_barrier_label": "KEEP: PT/SL/time labeling",
    "forward_returns": "KEEP: raw edge discovery target",
    "ablation_delta": "KEEP: feature contribution accounting",
}
