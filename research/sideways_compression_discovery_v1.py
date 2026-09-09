#!/usr/bin/env python3
"""Research-only discovery for Binance Spot long setups during sideways/compression regimes.

Purpose: explain/solve the current no-trade regime without weakening live gates.
This file NEVER authorizes live trading. It compares three long-only families:
1) compression breakout continuation,
2) range recovery after controlled pullback,
3) relative-strength leader reset/continuation.

Methodology:
- point-in-time features only
- deterministic 20% symbol holdout
- discovery/calibration/test time splits
- event de-clustering
- baseline + stressed round-trip costs
- MFE/MAE path metrics
- BH-FDR on discovery hypotheses
- fail-closed promotion because current-universe survivorship remains unresolved
"""
from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

from research import systematic_edge_scanner_v3 as core

# Keep this run bounded while the broad v3 scan runs independently.
core.DAYS = 180
core.MAX_SYMBOLS = 45
core.MIN_QV = 10_000_000
core.DOWNLOAD_WORKERS = 8

OUT = pathlib.Path('validation/edges/sideways-compression-v1-latest.json')
HORIZONS = (6, 12, 24)
BASE_COST = 0.0028
STRESS_COST = 0.0050
MIN_DISC = 35
MIN_VAL = 20
FDR_Q = 0.05
MIN_GAP_HOURS = 12


@dataclass(frozen=True)
class Hypothesis:
    family: str
    params: tuple

    @property
    def hid(self) -> str:
        return hashlib.sha1((self.family + ':' + ','.join(map(str, self.params))).encode()).hexdigest()[:12]


def hypotheses() -> list[Hypothesis]:
    out: list[Hypothesis] = []
    # Breakout from genuine low-volatility compression with strong flow and RS.
    for comp in (0.55, 0.70, 0.85):
        for rvol in (1.10, 1.35, 1.70):
            for rs in (0.65, 0.75, 0.85):
                out.append(Hypothesis('COMPRESSION_BREAKOUT', (comp, rvol, rs)))
    # Buy a controlled lower-range reset only after flow turns back positive.
    for dd in (-0.02, -0.035, -0.05):
        for taker in (0.54, 0.58, 0.62):
            for rs in (0.45, 0.60, 0.75):
                out.append(Hypothesis('RANGE_RECOVERY', (dd, taker, rs)))
    # Strong leaders that reset for 1h then re-accelerate, rather than chasing.
    for rs in (0.75, 0.85, 0.92):
        for pullback in (-0.004, -0.008, -0.012):
            for taker in (0.56, 0.60, 0.64):
                out.append(Hypothesis('LEADER_RESET', (rs, pullback, taker)))
    return out


def enrich(panel: pd.DataFrame) -> pd.DataFrame:
    p = panel.copy().sort_values(['symbol', 'ts']).reset_index(drop=True)
    g = p.groupby('symbol', group_keys=False)
    p['ret_1h'] = g.close.pct_change(1)
    p['ret_3h'] = g.close.pct_change(3)
    p['prev_24h_high'] = g.high.transform(lambda s: s.shift(1).rolling(24, min_periods=18).max())
    p['prev_24h_low'] = g.low.transform(lambda s: s.shift(1).rolling(24, min_periods=18).min())
    p['mid_24h'] = (p.prev_24h_high + p.prev_24h_low) / 2.0
    p['range_24h'] = p.prev_24h_high / p.prev_24h_low.replace(0, np.nan) - 1.0
    p['dd_from_24h_high'] = p.close / p.prev_24h_high - 1.0
    p['breakout_24h'] = p.close / p.prev_24h_high - 1.0
    p['rv_6h'] = g.ret_1h.transform(lambda s: s.rolling(6, min_periods=5).std(ddof=0))
    p['rv_24h'] = g.ret_1h.transform(lambda s: s.rolling(24, min_periods=18).std(ddof=0))
    p['compression_ratio'] = p.rv_6h / p.rv_24h.replace(0, np.nan)
    p['reclaim_3h'] = p.close > g.high.transform(lambda s: s.shift(1).rolling(3, min_periods=3).max())

    # Point-in-time breadth and BTC state. Regime label uses only information at t.
    grp = p.groupby('ts', sort=False)
    breadth6 = grp.ret_6h.apply(lambda s: float((s > 0).mean())).rename('breadth6')
    breadth1 = grp.ret_1h.apply(lambda s: float((s > 0).mean())).rename('breadth1')
    med6 = grp.ret_6h.median().rename('median6')
    ctx = pd.concat([breadth1, breadth6, med6], axis=1).reset_index()
    btc = p[p.symbol == 'BTCUSDT'][['ts', 'ret_6h', 'ret_24h', 'vol_ratio']].rename(columns={
        'ret_6h': 'btc6', 'ret_24h': 'btc24', 'vol_ratio': 'btc_vol_ratio'
    })
    ctx = ctx.merge(btc, on='ts', how='left')
    p = p.merge(ctx, on='ts', how='left')
    p['sideways'] = (
        p.btc6.abs().le(0.018)
        & p.btc24.abs().le(0.05)
        & p.breadth6.between(0.18, 0.68)
        & p.median6.abs().le(0.02)
        & p.btc_vol_ratio.le(1.40)
    )
    return p.replace([np.inf, -np.inf], np.nan)


def mask_for(df: pd.DataFrame, h: Hypothesis) -> pd.Series:
    base = df.sideways.fillna(False)
    if h.family == 'COMPRESSION_BREAKOUT':
        comp, rvol, rs = h.params
        return (
            base
            & (df.compression_ratio <= comp)
            & (df.breakout_24h >= 0.001)
            & (df.breakout_24h <= 0.012)
            & (df.rvol26 >= rvol)
            & (df.cs_rs_6h >= rs)
            & (df.taker_buy_ratio >= 0.56)
        )
    if h.family == 'RANGE_RECOVERY':
        dd, taker, rs = h.params
        return (
            base
            & (df.dd_from_24h_high <= dd)
            & (df.dd_from_24h_high >= -0.08)
            & (df.close >= df.mid_24h * 0.985)
            & (df.reclaim_3h)
            & (df.taker_buy_ratio >= taker)
            & (df.cs_rs_6h >= rs)
        )
    if h.family == 'LEADER_RESET':
        rs, pullback, taker = h.params
        return (
            base
            & (df.cs_rs_24h >= rs)
            & (df.ret_1h <= pullback)
            & (df.ret_1h >= -0.025)
            & (df.ret_3h > -0.03)
            & (df.close > df.mid_24h)
            & (df.taker_buy_ratio >= taker)
            & (df.rvol26 >= 0.90)
        )
    raise KeyError(h.family)


def holdout(symbol: str) -> bool:
    return int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16) % 100 < 20


def decluster(df: pd.DataFrame, mask: pd.Series) -> list[int]:
    out: list[int] = []
    last: dict[str, pd.Timestamp] = {}
    for i in np.flatnonzero(mask.fillna(False).to_numpy()):
        s = str(df.symbol.iloc[i]); ts = pd.Timestamp(df.ts.iloc[i])
        if s not in last or (ts - last[s]).total_seconds() >= MIN_GAP_HOURS * 3600:
            out.append(i); last[s] = ts
    return out


def paths(full: pd.DataFrame, subset: pd.DataFrame, h: Hypothesis, horizon: int) -> pd.DataFrame:
    rows = []
    for i in decluster(subset, mask_for(subset, h)):
        r = subset.iloc[i]; s = str(r.symbol); ts = pd.Timestamp(r.ts)
        z = full[(full.symbol == s) & (full.ts >= ts)].sort_values('ts')
        if len(z) <= horizon: continue
        entry = float(z.close.iloc[0]); fut = z.iloc[1:horizon+1]
        if entry <= 0 or len(fut) < horizon: continue
        rows.append({
            'symbol': s, 'ts': ts,
            'gross': float(z.close.iloc[horizon]) / entry - 1.0,
            'mfe': float(fut.high.max()) / entry - 1.0,
            'mae': max(0.0, 1.0 - float(fut.low.min()) / entry),
        })
    return pd.DataFrame(rows)


def pmean(a: np.ndarray) -> float:
    if len(a) < 2: return 1.0
    sd = float(np.std(a, ddof=1))
    if sd <= 0: return 0.0 if float(np.mean(a)) > 0 else 1.0
    z = float(np.mean(a)) / (sd / np.sqrt(len(a)))
    import math
    return 0.5 * math.erfc(z / np.sqrt(2.0))


def summary(ev: pd.DataFrame, cost: float) -> dict:
    if ev.empty: return {'n': 0}
    net = ev.gross.to_numpy(float) - cost
    mfe = ev.mfe.to_numpy(float); mae = ev.mae.to_numpy(float)
    losses = -net[net < 0]; wins = net[net > 0]
    return {
        'n': int(len(net)), 'meanNet': float(np.mean(net)), 'medianNet': float(np.median(net)),
        'hitRate': float(np.mean(net > 0)), 'pMeanGrossPositive': pmean(ev.gross.to_numpy(float)),
        'medianMFE': float(np.median(mfe)), 'medianMAE': float(np.median(mae)),
        'profitFactor': float(wins.sum() / losses.sum()) if losses.sum() > 0 else None,
    }


def bh(rows: list[dict]) -> None:
    ordered = sorted(enumerate(rows), key=lambda x: x[1]['discovery']['pMeanGrossPositive'])
    m = len(ordered); adjusted = [1.0] * m; running = 1.0
    for rank in range(m, 0, -1):
        idx, row = ordered[rank-1]; p = row['discovery']['pMeanGrossPositive']
        running = min(running, p * m / rank); adjusted[idx] = min(1.0, running)
    for i, row in enumerate(rows): row['qDiscovery'] = adjusted[i]


def main() -> None:
    syms = core.current_universe()[:core.MAX_SYMBOLS]
    data, failures = core.load_history(['BTCUSDT'] + syms)
    loaded = [s for s in syms if s in data]
    panel = enrich(core.build_panel(data, loaded))
    times = np.array(sorted(panel.ts.dropna().unique()))
    cut1, cut2 = times[int(len(times)*0.50)], times[int(len(times)*0.75)]
    train_symbols = {s for s in loaded if not holdout(s)}
    hold_symbols = {s for s in loaded if holdout(s)}

    disc = panel[(panel.ts < cut1) & panel.symbol.isin(train_symbols)]
    cal = panel[(panel.ts >= cut1) & (panel.ts < cut2) & panel.symbol.isin(train_symbols)]
    test = panel[(panel.ts >= cut2) & panel.symbol.isin(train_symbols)]
    unseen = panel[(panel.ts >= cut2) & panel.symbol.isin(hold_symbols)]

    rows = []
    for h in hypotheses():
        for horizon in HORIZONS:
            d = paths(panel, disc, h, horizon); ds = summary(d, BASE_COST)
            if ds['n'] < MIN_DISC: continue
            rows.append({'id': h.hid, 'family': h.family, 'params': list(h.params), 'horizonHours': horizon,
                         'discovery': ds, '_hyp': h})
    bh(rows)

    survivors = []
    for row in rows:
        ds = row['discovery']
        if not (row['qDiscovery'] <= FDR_Q and ds['meanNet'] > 0.0025 and ds['medianNet'] > 0 and ds['hitRate'] > 0.53):
            continue
        h = row.pop('_hyp'); hz = row['horizonHours']
        c = summary(paths(panel, cal, h, hz), BASE_COST)
        t = summary(paths(panel, test, h, hz), BASE_COST)
        u = summary(paths(panel, unseen, h, hz), BASE_COST)
        stress = summary(paths(panel, test, h, hz), STRESS_COST)
        row.update({'calibration': c, 'test': t, 'unseenSymbols': u, 'stressTest': stress})
        row['statisticalPass'] = bool(
            c.get('n',0) >= MIN_VAL and t.get('n',0) >= MIN_VAL
            and c.get('meanNet',-1) > 0 and t.get('meanNet',-1) > 0
            and stress.get('meanNet',-1) > 0
            and (u.get('n',0) < MIN_VAL or u.get('meanNet',-1) > 0)
        )
        # Fail closed: current universe introduces survivorship bias.
        row['liveEligible'] = False
        row['promotionBlocker'] = 'HISTORICAL_POINT_IN_TIME_UNIVERSE_AND_DELISTED_ASSET_COVERAGE_UNRESOLVED'
        survivors.append(row)

    report = {
        'engine': 'SIDEWAYS_COMPRESSION_DISCOVERY_V1', 'authorization': 'RESEARCH_ONLY', 'liveTrading': False,
        'days': core.DAYS, 'symbolsRequested': syms, 'symbolsLoaded': loaded, 'failures': failures,
        'regimeDefinition': 'BTC abs(6h)<=1.8%, abs(24h)<=5%, breadth6h 18-68%, median6h abs<=2%, BTC vol_ratio<=1.40',
        'families': ['COMPRESSION_BREAKOUT','RANGE_RECOVERY','LEADER_RESET'],
        'methodology': {'timeSplit':'50/25/25','symbolHoldoutPct':20,'BH_FDR_q':FDR_Q,'baselineCost':BASE_COST,'stressCost':STRESS_COST},
        'hypothesisTests': len(rows), 'discoverySurvivors': len(survivors),
        'statisticalPassCount': sum(1 for r in survivors if r.get('statisticalPass')),
        'liveEligibleCount': 0,
        'survivors': sorted(survivors, key=lambda r: (not r.get('statisticalPass',False), -r.get('test',{}).get('meanNet',-99))),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True, default=str, allow_nan=False), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('engine','symbolsLoaded','hypothesisTests','discoverySurvivors','statisticalPassCount','liveEligibleCount')}, indent=2))


if __name__ == '__main__':
    main()
