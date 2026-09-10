#!/usr/bin/env python3
"""Research-only validation for a proposed mid-momentum continuation lane.

Question: do Spot coins already up +5%..+25% over 24h have a repeatable long
edge after a controlled reset/reclaim, without weakening the existing live bot?

This script NEVER enables or changes live trading. It uses:
- 15m data; signal at bar close, entry at next 15m open
- untouched raw OHLC for TP/SL traversal
- same-bar TP+SL => SL first (pessimistic)
- chronological 60/20/20 discovery/calibration/test
- deterministic 20% symbol holdout, evaluated only in the final time window
- event de-clustering to reduce overlapping pseudo-trades
- fixed setup families and fixed TP/SL contracts
- BH-FDR on discovery hypotheses
- baseline and stressed round-trip costs
- day-cluster bootstrap on test results
- fail-closed production block because current-listing survivorship remains
"""
from __future__ import annotations

import hashlib
import json
import math
import pathlib
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from research import intraday_barrier_edge_scanner_v1 as base

base.DAYS = 180
base.MAX_SYMBOLS = 70
base.CURRENT_MIN_QV = 6_000_000
base.EVENT_MIN_QV24 = 3_000_000
base.EXCLUDED = set(base.EXCLUDED) | {"U"}

OUT = pathlib.Path("validation/edges/mid-momentum-continuation-v1-latest.json")
COST_NORMAL = 0.0028
COST_STRESS = 0.0050
FDR_Q = 0.10
MIN_DISC = 50
MIN_VAL = 20
DECLUSTER_BARS = 16
BOOTSTRAP_N = 1000
SEED = 20260910

CONTRACTS = {
    "TP15_SL10_4H": {"tp": 0.015, "sl": 0.010, "horizon_bars": 16},
    "TP22_SL13_8H": {"tp": 0.022, "sl": 0.013, "horizon_bars": 32},
    "TP30_SL17_12H": {"tp": 0.030, "sl": 0.017, "horizon_bars": 48},
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def augment(raw: pd.DataFrame, btc: pd.DataFrame) -> pd.DataFrame:
    x = raw.sort_values("ts").copy()
    c = x.close
    x["ret_15m"] = c.pct_change()
    x["ret_1h"] = c.pct_change(4)
    x["ret_4h"] = c.pct_change(16)
    x["ret_24h"] = c.pct_change(96)
    x["prev_ret_1h"] = x.ret_1h.shift(4)
    x["qv24"] = x.quote_volume.rolling(96, min_periods=48).sum()
    x["volume_ratio"] = x.quote_volume / x.quote_volume.rolling(32, min_periods=16).median().replace(0, np.nan)
    x["taker_buy_ratio"] = x.taker_quote / x.quote_volume.replace(0, np.nan)
    x["ema9"] = c.ewm(span=9, adjust=False).mean()
    x["ema21"] = c.ewm(span=21, adjust=False).mean()
    x["ema50"] = c.ewm(span=50, adjust=False).mean()
    x["rsi14"] = rsi(c)

    hi8 = x.high.rolling(32, min_periods=16).max()
    x["pullback_8h"] = c / hi8 - 1.0
    lo1 = x.low.rolling(4, min_periods=4).min()
    x["bounce_1h"] = c / lo1 - 1.0
    prev_hi4 = x.high.rolling(16, min_periods=12).max().shift(1)
    prev_lo4 = x.low.rolling(16, min_periods=12).min().shift(1)
    x["breakout_4h"] = c / prev_hi4 - 1.0
    x["range_4h"] = prev_hi4 / prev_lo4.replace(0, np.nan) - 1.0

    r = c.pct_change()
    rv4 = r.rolling(16, min_periods=12).std(ddof=0)
    rv24 = r.rolling(96, min_periods=64).std(ddof=0)
    x["compression"] = rv4 / rv24.replace(0, np.nan)

    body = (x.close - x.open).abs()
    x["upper_wick_ratio"] = (x.high - x[["open", "close"]].max(axis=1)) / np.maximum(body, c * 0.0001)

    b = btc[["ts", "close"]].rename(columns={"close": "btc_close"})
    x = x.merge(b, on="ts", how="left")
    x["btc_close"] = x.btc_close.ffill()
    x["btc_ret_4h"] = x.btc_close.pct_change(16)
    x["btc_ret_24h"] = x.btc_close.pct_change(96)
    x["rs_4h"] = x.ret_4h - x.btc_ret_4h
    return x.replace([np.inf, -np.inf], np.nan)


def cross_sectional(panel: pd.DataFrame) -> pd.DataFrame:
    p = panel.copy()
    for col in ("ret_4h", "ret_24h", "rs_4h", "volume_ratio", "taker_buy_ratio"):
        p[col + "_rank"] = p.groupby("ts", sort=False)[col].rank(pct=True, method="average")
    p["breadth_4h"] = p.groupby("ts", sort=False).ret_4h.transform(lambda s: float((s > 0).mean()))
    return p


def masks(p: pd.DataFrame) -> dict[str, pd.Series]:
    mid = (
        p.ret_24h.between(0.05, 0.25)
        & (p.qv24 >= base.EVENT_MIN_QV24)
        & (p.btc_ret_4h > -0.025)
        & (p.btc_ret_24h > -0.05)
        & (p.ret_15m < 0.025)
        & (p.rsi14 <= 78)
    )

    pullback_reclaim = (
        mid
        & p.pullback_8h.between(-0.05, -0.008)
        & (p.bounce_1h >= 0.002)
        & (p.ret_15m > 0)
        & (p.close >= p.ema9)
        & (p.ema9 >= p.ema21 * 0.995)
        & (p.rs_4h_rank >= 0.65)
        & (p.volume_ratio >= 0.90)
        & (p.taker_buy_ratio >= 0.56)
        & p.rsi14.between(45, 74)
    )

    reacceleration = (
        mid
        & p.prev_ret_1h.between(-0.025, 0.005)
        & p.ret_1h.between(0.002, 0.025)
        & p.pullback_8h.between(-0.045, -0.002)
        & (p.close >= p.ema21 * 0.998)
        & (p.ema21 >= p.ema50 * 0.992)
        & (p.rs_4h_rank >= 0.70)
        & (p.volume_ratio >= 1.10)
        & (p.taker_buy_ratio >= 0.58)
        & (p.upper_wick_ratio <= 1.8)
        & (p.rsi14 <= 74)
    )

    range_resume = (
        mid
        & (p.compression <= 0.85)
        & (p.range_4h <= 0.05)
        & p.breakout_4h.between(0.0, 0.012)
        & (p.ret_15m > 0)
        & (p.volume_ratio >= 1.25)
        & (p.taker_buy_ratio >= 0.58)
        & (p.rs_4h_rank >= 0.65)
        & (p.upper_wick_ratio <= 1.25)
        & (p.rsi14 <= 76)
    )

    return {
        "MID_PULLBACK_RECLAIM": pullback_reclaim,
        "MID_REACCELERATION": reacceleration,
        "MID_RANGE_RESUME": range_resume,
    }


def holdout(symbol: str) -> bool:
    return int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16) % 5 == 0


def event_rows(p: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    z = p.loc[mask.fillna(False)].sort_values(["symbol", "ts"]).copy()
    if z.empty:
        return z
    keep = []
    last: dict[str, pd.Timestamp] = {}
    for i, r in z.iterrows():
        s = str(r.symbol)
        ts = pd.Timestamp(r.ts)
        if s not in last or (ts - last[s]).total_seconds() >= DECLUSTER_BARS * 15 * 60:
            keep.append(i)
            last[s] = ts
    return z.loc[keep]


def barrier(raw: pd.DataFrame, idx: int, c: dict, event_ts, regime: str):
    if idx + 1 >= len(raw):
        return None
    entry = float(raw.open.iloc[idx + 1])
    if not math.isfinite(entry) or entry <= 0:
        return None
    tp = entry * (1 + c["tp"])
    sl = entry * (1 - c["sl"])
    end = min(len(raw) - 1, idx + c["horizon_bars"])
    gross = None
    outcome = "TIMEOUT"
    mfe = 0.0
    mae = 0.0
    for j in range(idx + 1, end + 1):
        hi = float(raw.high.iloc[j])
        lo = float(raw.low.iloc[j])
        mfe = max(mfe, hi / entry - 1.0)
        mae = max(mae, 1.0 - lo / entry)
        hit_tp = hi >= tp
        hit_sl = lo <= sl
        if hit_tp and hit_sl:
            gross = -c["sl"]
            outcome = "SL_AMBIGUOUS"
            break
        if hit_sl:
            gross = -c["sl"]
            outcome = "SL"
            break
        if hit_tp:
            gross = c["tp"]
            outcome = "TP"
            break
    if gross is None:
        gross = max(-c["sl"], min(c["tp"], float(raw.close.iloc[end]) / entry - 1.0))
    return {
        "gross": gross,
        "outcome": outcome,
        "mfe": mfe,
        "mae": mae,
        "day": pd.Timestamp(event_ts).strftime("%Y-%m-%d"),
        "regime": regime,
    }


def summarize(rows: list[dict], cost: float) -> dict:
    if not rows:
        return {"n": 0}
    net = np.asarray([r["gross"] - cost for r in rows], dtype=float)
    pos = np.maximum(net, 0).sum()
    neg = np.maximum(-net, 0).sum()
    return {
        "n": len(rows),
        "meanNet": float(net.mean()),
        "medianNet": float(np.median(net)),
        "profitFactor": float(pos / neg) if neg > 0 else (999.0 if pos > 0 else 0.0),
        "tpRate": float(np.mean([r["outcome"] == "TP" for r in rows])),
        "slRate": float(np.mean([r["outcome"] in {"SL", "SL_AMBIGUOUS"} for r in rows])),
        "medianMFE": float(np.median([r["mfe"] for r in rows])),
        "medianMAE": float(np.median([r["mae"] for r in rows])),
    }


def pmean(rows: list[dict], cost: float) -> float:
    if len(rows) < 2:
        return 1.0
    a = np.asarray([r["gross"] - cost for r in rows], dtype=float)
    sd = float(a.std(ddof=1))
    if sd <= 0:
        return 0.0 if float(a.mean()) > 0 else 1.0
    z = float(a.mean()) / (sd / math.sqrt(len(a)))
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def bootstrap(rows: list[dict], cost: float) -> dict:
    by = defaultdict(list)
    for r in rows:
        by[r["day"]].append(r["gross"] - cost)
    days = sorted(by)
    if len(rows) < MIN_VAL or len(days) < 10:
        return {"status": "INSUFFICIENT", "n": len(rows), "days": len(days)}
    rng = np.random.default_rng(SEED)
    means = []
    for _ in range(BOOTSTRAP_N):
        vals = []
        for day in rng.choice(days, size=len(days), replace=True):
            vals.extend(by[str(day)])
        means.append(float(np.mean(vals)))
    return {
        "status": "OK",
        "days": len(days),
        "p05": float(np.quantile(means, 0.05)),
        "p50": float(np.quantile(means, 0.50)),
        "p95": float(np.quantile(means, 0.95)),
    }


def bh(rows: list[dict]) -> None:
    order = sorted(range(len(rows)), key=lambda i: rows[i]["discoveryP"])
    m = len(order)
    adj = [1.0] * len(rows)
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, rows[i]["discoveryP"] * m / rank)
        adj[i] = min(1.0, running)
    for i, row in enumerate(rows):
        row["qValue"] = adj[i]


def valid(s: dict, n: int = MIN_VAL) -> bool:
    return bool(s.get("n", 0) >= n and s.get("meanNet", -1) > 0 and s.get("profitFactor", 0) >= 1.05)


def main() -> None:
    started = time.time()
    syms = base.universe()
    all_syms = syms if "BTCUSDT" in syms else ["BTCUSDT"] + syms
    data = {}
    failures = {}
    log(f"universe={len(syms)}")
    with ThreadPoolExecutor(max_workers=base.DOWNLOAD_WORKERS) as ex:
        fut = {ex.submit(base.klines, s): s for s in all_syms}
        for n, f in enumerate(as_completed(fut), 1):
            s = fut[f]
            try:
                data[s] = f.result()
                log(f"data {n}/{len(all_syms)} {s} bars={len(data[s])}")
            except Exception as exc:
                failures[s] = str(exc)
                log(f"data {n}/{len(all_syms)} {s} FAIL {exc}")
    if "BTCUSDT" not in data:
        raise RuntimeError("BTC history unavailable")

    loaded = [s for s in syms if s in data and s != "BTCUSDT"]
    frames = [augment(data[s], data["BTCUSDT"]) for s in loaded]
    p = cross_sectional(pd.concat(frames, ignore_index=True))
    p = p.dropna(subset=["ret_15m", "ret_1h", "ret_4h", "ret_24h", "qv24", "volume_ratio", "taker_buy_ratio", "rsi14", "btc_ret_4h", "btc_ret_24h"])
    p = p.sort_values(["symbol", "ts"]).reset_index(drop=True)
    times = sorted(p.ts.unique())
    t60 = times[int(len(times) * 0.60)]
    t80 = times[int(len(times) * 0.80)]
    p["holdout"] = p.symbol.map(holdout)
    p["split"] = np.where(p.ts < t60, "DISCOVERY", np.where(p.ts < t80, "CALIBRATION", "TEST"))
    p["regime"] = np.select(
        [p.btc_ret_4h > 0.01, p.btc_ret_4h < -0.01, p.breadth_4h >= 0.60, p.breadth_4h <= 0.40],
        ["BTC_UP", "BTC_DOWN", "BROAD_UP", "BROAD_DOWN"],
        default="MIXED",
    )

    raw = {s: data[s].sort_values("ts").reset_index(drop=True) for s in loaded}
    lookup = {s: {pd.Timestamp(t): i for i, t in enumerate(g.ts)} for s, g in raw.items()}

    results = []
    for family, mask in masks(p).items():
        selected = event_rows(p, mask)
        for cname, contract in CONTRACTS.items():
            buckets = {"DISCOVERY": [], "CALIBRATION": [], "TEST": [], "STRICT_HOLDOUT": []}
            for r in selected.itertuples(index=False):
                if r.holdout:
                    if r.split != "TEST":
                        continue
                    bucket = "STRICT_HOLDOUT"
                else:
                    bucket = r.split
                idx = lookup[r.symbol].get(pd.Timestamp(r.ts))
                if idx is None:
                    continue
                z = barrier(raw[r.symbol], idx, contract, r.ts, r.regime)
                if z:
                    buckets[bucket].append(z)
            normal = {k: summarize(v, COST_NORMAL) for k, v in buckets.items()}
            stress = {k: summarize(v, COST_STRESS) for k, v in buckets.items()}
            results.append({
                "family": family,
                "contract": cname,
                "discoveryP": pmean(buckets["DISCOVERY"], COST_NORMAL),
                "normal": normal,
                "stress": stress,
                "testBootstrap": bootstrap(buckets["TEST"], COST_NORMAL),
                "holdoutBootstrap": bootstrap(buckets["STRICT_HOLDOUT"], COST_NORMAL),
                "testRegimes": {k: summarize([r for r in buckets["TEST"] if r["regime"] == k], COST_NORMAL) for k in sorted({r["regime"] for r in buckets["TEST"]})},
            })

    bh(results)
    for h in results:
        n = h["normal"]
        st = h["stress"]
        h["discoveryPass"] = bool(
            n["DISCOVERY"].get("n", 0) >= MIN_DISC
            and n["DISCOVERY"].get("meanNet", -1) > 0
            and n["DISCOVERY"].get("profitFactor", 0) >= 1.15
            and h["qValue"] <= FDR_Q
        )
        h["calibrationPass"] = valid(n["CALIBRATION"])
        h["testPass"] = valid(n["TEST"])
        h["strictHoldoutPass"] = valid(n["STRICT_HOLDOUT"])
        h["stressPass"] = valid(st["TEST"]) and valid(st["STRICT_HOLDOUT"])
        h["bootstrapPass"] = h["testBootstrap"].get("status") == "OK" and h["testBootstrap"].get("p05", -1) > 0
        h["holdoutBootstrapPass"] = h["holdoutBootstrap"].get("status") == "OK" and h["holdoutBootstrap"].get("p05", -1) > 0
        h["statisticalEvidencePass"] = all([
            h["discoveryPass"], h["calibrationPass"], h["testPass"], h["strictHoldoutPass"],
            h["stressPass"], h["bootstrapPass"], h["holdoutBootstrapPass"],
        ])
        h["productionEligible"] = False

    survivors = [h for h in results if h["statisticalEvidencePass"]]
    ranked = sorted(results, key=lambda h: h["normal"]["TEST"].get("meanNet", -99), reverse=True)
    payload = {
        "engine": "MID_MOMENTUM_CONTINUATION_V1",
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "automaticPromotion": False,
        "productionPromotionBlocked": True,
        "productionBlockers": ["CURRENT_LISTING_SURVIVORSHIP_NOT_ELIMINATED", "WALK_FORWARD_NOT_IN_THIS_RUN"],
        "question": "Validate a separate +5%..+25% 24h continuation lane before any live change",
        "days": base.DAYS,
        "interval": base.INTERVAL,
        "symbolsRequested": syms,
        "symbolsLoaded": loaded,
        "failures": failures,
        "validationDesign": {
            "entryTiming": "NEXT_15M_OPEN",
            "sameBarTpSlResolution": "SL_FIRST_PESSIMISTIC",
            "timeSplit": "60/20/20",
            "strictSymbolHoldout": "20% SHA256 holdout; final 20% time only",
            "eventDeclusterHours": 4,
            "costNormal": COST_NORMAL,
            "costStress": COST_STRESS,
            "BH_FDR_q": FDR_Q,
            "bootstrap": "day-cluster",
        },
        "hypothesisCount": len(results),
        "survivorCount": len(survivors),
        "survivors": survivors,
        "rankedDiagnostics": ranked,
        "runtimeSeconds": round(time.time() - started, 2),
        "note": "No live rule is changed by this report, even if a statistical candidate passes.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    log(f"DONE hypotheses={len(results)} survivors={len(survivors)}")


if __name__ == "__main__":
    main()
