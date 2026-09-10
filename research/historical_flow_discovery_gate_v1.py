#!/usr/bin/env python3
"""Research-only discovery gate for historical Binance flow features.

Consumes one or more 5m Parquet files from the frozen historical feature layer.
Thresholds are calibrated on the first 60% of each symbol and evaluated only on
the remaining 40%. No TP/SL, sizing, execution or live-trading logic.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import pathlib

import numpy as np
import pandas as pd
from scipy import stats

AUTHORIZATION = "RESEARCH_ONLY"
TRAIN_FRAC = 0.60
LOW_Q = 0.10
HIGH_Q = 0.90
FDR_ALPHA = 0.05
MIN_EVENTS = 30
HORIZONS = {"15m": 3, "1h": 12, "4h": 48}
FEATURES = [
    "flow_imbalance_quote",
    "buy_share_quote",
    "taker_buy_sell_ratio_quote",
    "mark_index_basis_bps",
    "premium_close",
    "agg_trade_count",
]


def bh_adjust(pvals: list[float]) -> list[float]:
    n = len(pvals)
    order = np.argsort(np.asarray(pvals, dtype=float))
    out = np.ones(n, dtype=float)
    prev = 1.0
    for rank0 in range(n - 1, -1, -1):
        idx = int(order[rank0])
        rank = rank0 + 1
        q = min(prev, pvals[idx] * n / rank)
        out[idx] = min(1.0, q)
        prev = q
    return out.tolist()


def finite(a) -> np.ndarray:
    x = np.asarray(a, dtype=float)
    return x[np.isfinite(x)]


def one_sided_p_positive(x: np.ndarray) -> float:
    x = finite(x)
    if len(x) < 2 or float(np.std(x, ddof=1)) == 0.0:
        return 1.0
    res = stats.ttest_1samp(x, popmean=0.0, alternative="greater", nan_policy="omit")
    return float(res.pvalue) if math.isfinite(float(res.pvalue)) else 1.0


def metric_row(x: np.ndarray) -> dict:
    x = finite(x)
    if len(x) == 0:
        return {"n": 0, "mean": None, "median": None, "hitRate": None, "pValue": 1.0}
    return {
        "n": int(len(x)),
        "mean": float(np.mean(x)),
        "median": float(np.median(x)),
        "hitRate": float(np.mean(x > 0)),
        "pValue": one_sided_p_positive(x),
    }


def load_files(pattern: str) -> list[pd.DataFrame]:
    paths = sorted(glob.glob(pattern, recursive=True))
    if not paths:
        raise SystemExit(f"no parquet files matched: {pattern}")
    out = []
    for p in paths:
        d = pd.read_parquet(p)
        if "symbol" not in d.columns or "ts" not in d.columns or "trade_close" not in d.columns:
            raise RuntimeError(f"missing canonical columns in {p}")
        d = d.copy()
        d["ts"] = pd.to_datetime(d["ts"], utc=True)
        out.append(d.sort_values(["symbol", "ts"]).reset_index(drop=True))
    return out


def symbol_tests(d: pd.DataFrame) -> tuple[list[dict], dict]:
    symbol = str(d["symbol"].iloc[0])
    if d["symbol"].nunique() != 1:
        raise RuntimeError("each parquet must contain exactly one symbol")
    n = len(d)
    cut = max(1, int(n * TRAIN_FRAC))
    train = d.iloc[:cut].copy()
    test = d.iloc[cut:].copy()
    thresholds = {}
    rows = []

    for hname, bars in HORIZONS.items():
        test[f"fwd_{hname}"] = test["trade_close"].shift(-bars) / test["trade_close"] - 1.0

    for feature in FEATURES:
        if feature not in d.columns:
            continue
        tr = pd.to_numeric(train[feature], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        if len(tr) < 50:
            continue
        lo = float(tr.quantile(LOW_Q))
        hi = float(tr.quantile(HIGH_Q))
        thresholds[feature] = {"q10": lo, "q90": hi}
        tv = pd.to_numeric(test[feature], errors="coerce")
        tail_masks = {"LOW": tv <= lo, "HIGH": tv >= hi}

        for tail, mask in tail_masks.items():
            for mode, direction in (("CONTINUATION", 1 if tail == "HIGH" else -1), ("REVERSAL", -1 if tail == "HIGH" else 1)):
                for hname in HORIZONS:
                    raw = pd.to_numeric(test.loc[mask, f"fwd_{hname}"], errors="coerce").to_numpy(dtype=float)
                    directed = finite(raw) * direction
                    m = metric_row(directed)
                    rows.append({
                        "symbol": symbol,
                        "feature": feature,
                        "tail": tail,
                        "mode": mode,
                        "direction": "LONG" if direction == 1 else "SHORT",
                        "horizon": hname,
                        "threshold": lo if tail == "LOW" else hi,
                        **m,
                    })

    meta = {
        "symbol": symbol,
        "rows": n,
        "trainRows": len(train),
        "testRows": len(test),
        "trainStart": str(train["ts"].min()),
        "trainEnd": str(train["ts"].max()),
        "testStart": str(test["ts"].min()),
        "testEnd": str(test["ts"].max()),
        "thresholds": thresholds,
    }
    return rows, meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="artifacts/historical-flow-input/**/*.parquet")
    ap.add_argument("--output", default="validation/edges/historical-flow-discovery-gate-v1.json")
    args = ap.parse_args()

    tests = []
    symbols = []
    for d in load_files(args.input):
        rows, meta = symbol_tests(d)
        tests.extend(rows)
        symbols.append(meta)

    qvals = bh_adjust([float(r["pValue"]) for r in tests]) if tests else []
    for r, q in zip(tests, qvals):
        r["qValueBH"] = q
        r["statisticalPass"] = bool(
            r["n"] >= MIN_EVENTS
            and r["mean"] is not None and r["mean"] > 0
            and r["median"] is not None and r["median"] > 0
            and r["hitRate"] is not None and r["hitRate"] > 0.55
            and q <= FDR_ALPHA
        )

    survivors = [r for r in tests if r["statisticalPass"]]
    survivors.sort(key=lambda r: (r["qValueBH"], -(r["mean"] or 0)))
    payload = {
        "engine": "HISTORICAL_FLOW_DISCOVERY_GATE_V1",
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "automaticPromotion": False,
        "researchStage": "DISCOVERY_ONLY",
        "dataSplit": {"trainFraction": TRAIN_FRAC, "testFraction": 1 - TRAIN_FRAC},
        "frozenQuantiles": {"low": LOW_Q, "high": HIGH_Q},
        "horizons": HORIZONS,
        "features": FEATURES,
        "multipleTesting": {"method": "Benjamini-Hochberg", "alpha": FDR_ALPHA},
        "gate": {"minEvents": MIN_EVENTS, "median": ">0", "hitRate": ">0.55", "bhFdr": "<=0.05"},
        "symbols": symbols,
        "testCount": len(tests),
        "survivorCount": len(survivors),
        "survivors": survivors,
        "tests": tests,
        "nextStep": "Any discovery survivor must be frozen and validated on a disjoint date range and unseen-symbol holdout before strategy construction.",
    }
    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    print(json.dumps({
        "engine": payload["engine"],
        "symbols": len(symbols),
        "tests": len(tests),
        "survivors": len(survivors),
        "top": survivors[:8],
    }, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
