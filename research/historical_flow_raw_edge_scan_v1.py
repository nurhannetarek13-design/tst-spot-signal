#!/usr/bin/env python3
"""Research-only raw edge scan over Binance Vision historical flow features.

No TP/SL, no position sizing, no live trading, and no threshold optimization.
The event definitions are frozen before OOS: trailing 24h z-score tails at +/-2
on stationary flow/basis/activity features. Discovery and OOS are split by date.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
from datetime import datetime

import numpy as np
import pandas as pd

from research.binance_vision_historical_feature_layer import build

AUTHORIZATION = "RESEARCH_ONLY"
ZSCORE_WINDOW = 288  # 24h at 5m
ZSCORE_MIN = 144
Z_TAIL = 2.0
HORIZONS = {"15m": 3, "60m": 12, "240m": 48}
MIN_DISCOVERY_EVENTS = 20
MIN_OOS_EVENTS = 15
FEATURES = [
    "flow_imbalance_quote",
    "flow_1h",
    "basis_bps",
    "premium_close",
    "trade_count_z",
    "range_z",
]


def rolling_z(s: pd.Series) -> pd.Series:
    mean = s.rolling(ZSCORE_WINDOW, min_periods=ZSCORE_MIN).mean()
    std = s.rolling(ZSCORE_WINDOW, min_periods=ZSCORE_MIN).std(ddof=0).replace(0, np.nan)
    return (s - mean) / std


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    x = df.sort_values("ts").copy()
    x["ts"] = pd.to_datetime(x["ts"], utc=True)
    total_1h = x["total_quote"].rolling(12, min_periods=12).sum()
    delta_1h = x["delta_quote"].rolling(12, min_periods=12).sum()
    x["flow_1h"] = delta_1h / total_1h.replace(0, np.nan)
    x["basis_bps"] = x["mark_index_basis_bps"]
    x["trade_count_z"] = rolling_z(np.log1p(x["agg_trade_count"].astype(float)))
    x["range_z"] = rolling_z(x["realized_range_bps"].astype(float))
    # Normalized features get trailing z-scores too so the threshold is comparable.
    for c in ("flow_imbalance_quote", "flow_1h", "basis_bps", "premium_close"):
        x[c + "__z"] = rolling_z(x[c].astype(float))
    x["trade_count_z__z"] = x["trade_count_z"]
    x["range_z__z"] = x["range_z"]
    for name, bars in HORIZONS.items():
        x["fwd_" + name] = x["trade_close"].shift(-bars) / x["trade_close"] - 1.0
    return x


def metrics(vals: pd.Series) -> dict:
    v = pd.to_numeric(vals, errors="coerce").dropna().astype(float)
    if len(v) == 0:
        return {"n": 0}
    pos = float(v[v > 0].sum())
    neg = float(-v[v < 0].sum())
    pf = pos / neg if neg > 0 else (999.0 if pos > 0 else 0.0)
    return {
        "n": int(len(v)),
        "mean": float(v.mean()),
        "median": float(v.median()),
        "hitRate": float((v > 0).mean()),
        "profitFactor": float(pf),
    }


def event_mask(x: pd.DataFrame, feature: str, side: str) -> pd.Series:
    z = x[feature + "__z"]
    return z >= Z_TAIL if side == "HIGH" else z <= -Z_TAIL


def gate(m: dict, min_events: int) -> bool:
    return bool(
        m.get("n", 0) >= min_events
        and m.get("mean", -1) > 0
        and m.get("median", -1) > 0
        and m.get("hitRate", 0) > 0.55
        and m.get("profitFactor", 0) >= 1.20
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--split", default="2025-01-08")
    p.add_argument("--end", default="2025-01-14")
    p.add_argument("--output-dir", default="artifacts/historical-flow-raw-edge")
    args = p.parse_args()

    symbol = args.symbol.upper()
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    split = pd.Timestamp(args.split, tz="UTC")
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    raw, meta = build(symbol, start, end, "5min", True)
    if meta["failures"]:
        raise RuntimeError(f"archive failures: {meta['failures'][:5]}")
    x = prepare(raw)
    discovery = x[x["ts"] < split].copy()
    oos = x[x["ts"] >= split].copy()

    tests = []
    for feature in FEATURES:
        for side in ("HIGH", "LOW"):
            dm = event_mask(discovery, feature, side)
            om = event_mask(oos, feature, side)
            for horizon in HORIZONS:
                dmet = metrics(discovery.loc[dm, "fwd_" + horizon])
                omet = metrics(oos.loc[om, "fwd_" + horizon])
                dpass = gate(dmet, MIN_DISCOVERY_EVENTS)
                opass = gate(omet, MIN_OOS_EVENTS)
                tests.append({
                    "feature": feature,
                    "side": side,
                    "threshold": f"z {'>=' if side == 'HIGH' else '<='} {Z_TAIL if side == 'HIGH' else -Z_TAIL}",
                    "horizon": horizon,
                    "discovery": {**dmet, "pass": dpass},
                    "oos": {**omet, "pass": opass},
                    "survivor": bool(dpass and opass),
                })

    survivors = [t for t in tests if t["survivor"]]
    report = {
        "engine": "HISTORICAL_FLOW_RAW_EDGE_SCAN_V1",
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "automaticPromotion": False,
        "symbol": symbol,
        "window": {"start": args.start, "split": args.split, "end": args.end},
        "frozenDefinition": {
            "frequency": "5min",
            "zWindowBars": ZSCORE_WINDOW,
            "zMinBars": ZSCORE_MIN,
            "zTail": Z_TAIL,
            "features": FEATURES,
            "horizons": HORIZONS,
            "noTpSl": True,
            "noSizing": True,
        },
        "rows": int(len(x)),
        "discoveryRows": int(len(discovery)),
        "oosRows": int(len(oos)),
        "tests": tests,
        "survivors": survivors,
        "survivorCount": len(survivors),
        "verdict": "SURVIVOR_FOUND" if survivors else "NO_STABLE_RAW_EDGE",
    }
    out = pathlib.Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{symbol}-raw-edge-v1.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "engine": report["engine"],
        "symbol": symbol,
        "rows": report["rows"],
        "tests": len(tests),
        "survivors": len(survivors),
        "verdict": report["verdict"],
        "topDiscovery": sorted(tests, key=lambda t: t["discovery"].get("mean", -999), reverse=True)[:5],
    }, separators=(",", ":")))


if __name__ == "__main__":
    main()
