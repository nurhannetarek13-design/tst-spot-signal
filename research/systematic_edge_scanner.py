#!/usr/bin/env python3
"""Systematic raw-edge discovery scanner for Binance Spot.

Research only. No strategy rules, sizing, execution, or live authorization.
Scans predeclared feature buckets and feature intersections, then applies the
existing raw forward-return gates plus Benjamini-Hochberg FDR control.
"""
from __future__ import annotations

import json, math, pathlib, time, urllib.parse, urllib.request
from itertools import product

import numpy as np
import pandas as pd

BASE_URL = "https://data-api.binance.vision"
INTERVAL = "1h"
DAYS = 365
MAX_SYMBOLS = 40
MIN_QV = 20_000_000
MAX_QV = 150_000_000
MAX_PRICE = 3.0
HORIZONS = (24, 48, 72)
MIN_EVENTS = 40
MIN_GAP = 12
FDR_Q = 0.05
OUT = pathlib.Path("validation/edges/systematic-edge-scanner-latest.json")

MAJORS = {"BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","LTC","BCH","LINK","AVAX","DOT"}
EXCLUDED = {"USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","AEUR","TRY","BRL","GBP","AUD","USD1","RLUSD","USDE","PAXG","XAUT"}

FEATURES = (
    "rvol26",
    "taker_buy_ratio",
    "rel_strength_6h",
    "rel_strength_24h",
    "vol_ratio_24_168",
    "ret_6h",
    "ret_24h",
    "drawdown_72h",
)


def api(path: str):
    req = urllib.request.Request(BASE_URL + path, headers={"User-Agent": "tst-systematic-edge-scanner/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def universe():
    info = api("/api/v3/exchangeInfo")
    ticker = {x["symbol"]: x for x in api("/api/v3/ticker/24hr")}
    rows = []
    for s in info.get("symbols", []):
        base = s.get("baseAsset", "")
        if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT" or not s.get("isSpotTradingAllowed"):
            continue
        if not base or base in MAJORS or base in EXCLUDED or base.endswith(("UP","DOWN","BULL","BEAR")):
            continue
        t = ticker.get(s["symbol"], {})
        px = float(t.get("lastPrice") or 0)
        qv = float(t.get("quoteVolume") or 0)
        if 0 < px <= MAX_PRICE and MIN_QV <= qv <= MAX_QV:
            rows.append((s["symbol"], qv))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:MAX_SYMBOLS]]


def klines(symbol: str):
    end = int(time.time() * 1000)
    cur = end - DAYS * 86400000
    rows = []
    while cur < end:
        q = urllib.parse.urlencode({"symbol": symbol, "interval": INTERVAL, "limit": 1000, "startTime": cur, "endTime": end})
        batch = api("/api/v3/klines?" + q)
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1][0]) + 3600000
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.01)
    if len(rows) < 4000:
        raise RuntimeError(f"{symbol}: insufficient bars {len(rows)}")
    df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"])
    for c in ["open","high","low","close","volume","quote_volume","taker_quote"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("ts")[["open","high","low","close","volume","quote_volume","taker_quote"]].dropna()


def build_features(df: pd.DataFrame, btc_close: pd.Series):
    c = df.close
    btc = btc_close.reindex(df.index).ffill()
    r1 = c.pct_change()
    btc1 = btc.pct_change()
    rv24 = r1.rolling(24).std(ddof=0)
    rv168 = r1.rolling(168).std(ddof=0)
    peak72 = c.rolling(72).max()
    out = pd.DataFrame(index=df.index)
    out["rvol26"] = df.volume / df.volume.rolling(26).mean().replace(0, np.nan)
    out["taker_buy_ratio"] = df.taker_quote / df.quote_volume.replace(0, np.nan)
    out["rel_strength_6h"] = c.pct_change(6) - btc.pct_change(6)
    out["rel_strength_24h"] = c.pct_change(24) - btc.pct_change(24)
    out["vol_ratio_24_168"] = rv24 / rv168.replace(0, np.nan)
    out["ret_6h"] = c.pct_change(6)
    out["ret_24h"] = c.pct_change(24)
    out["drawdown_72h"] = c / peak72 - 1.0
    return out.replace([np.inf, -np.inf], np.nan)


def qbucket(s: pd.Series, q: int = 5):
    try:
        return pd.qcut(s, q=q, labels=False, duplicates="drop")
    except ValueError:
        return pd.Series(np.nan, index=s.index)


def decluster(indices):
    out, last = [], -10**18
    for i in sorted(indices):
        if i - last >= MIN_GAP:
            out.append(i)
            last = i
    return out


def one_sided_mean_p(a: np.ndarray):
    if len(a) < 2:
        return 1.0
    sd = float(a.std(ddof=1))
    if sd <= 0:
        return 0.0 if float(a.mean()) > 0 else 1.0
    z = float(a.mean()) / (sd / math.sqrt(len(a)))
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def stats_for(df, indices, horizon):
    idx = decluster(indices)
    vals, mfes, maes = [], [], []
    for i in idx:
        if i + horizon >= len(df):
            continue
        e = float(df.close.iloc[i])
        vals.append(float(df.close.iloc[i + horizon]) / e - 1.0)
        mfes.append(float(df.high.iloc[i + 1:i + horizon + 1].max()) / e - 1.0)
        maes.append(max(0.0, 1.0 - float(df.low.iloc[i + 1:i + horizon + 1].min()) / e))
    if not vals:
        return None
    a = np.asarray(vals, float)
    m = np.asarray(mfes, float)
    d = np.asarray(maes, float)
    med_mae = float(np.median(d))
    ratio = float(np.median(m)) / med_mae if med_mae > 0 else (float("inf") if float(np.median(m)) > 0 else 0.0)
    mean = float(a.mean())
    median = float(np.median(a))
    hit = float((a > 0).mean())
    raw_pass = bool(len(a) >= MIN_EVENTS and mean >= 0.015 and median > 0 and hit > 0.55 and ratio >= 2.0)
    return {"n": int(len(a)), "mean": mean, "median": median, "hitRate": hit,
            "medianMFE": float(np.median(m)), "medianMAE": med_mae, "mfeMaeRatio": ratio,
            "pMeanPositive": one_sided_mean_p(a), "rawPass": raw_pass}


def bh_adjust(rows):
    ps = [(i, r["pMeanPositive"]) for i, r in enumerate(rows) if r.get("pMeanPositive") is not None]
    ps.sort(key=lambda x: x[1])
    m = len(ps)
    adj = [1.0] * len(rows)
    running = 1.0
    for rank in range(m, 0, -1):
        i, p = ps[rank - 1]
        running = min(running, p * m / rank)
        adj[i] = min(1.0, running)
    for i, r in enumerate(rows):
        r["qValue"] = adj[i]
        r["pass"] = bool(r.get("rawPass", False) and adj[i] <= FDR_Q)


def main():
    symbols = universe()
    data, failures = {}, {}
    for s in ["BTCUSDT"] + symbols:
        try:
            data[s] = klines(s)
        except Exception as e:
            failures[s] = str(e)
    if "BTCUSDT" not in data:
        raise RuntimeError("BTCUSDT history unavailable")

    tests = []
    for s in symbols:
        if s not in data:
            continue
        df = data[s]
        feats = build_features(df, data["BTCUSDT"].close)
        buckets = {f: qbucket(feats[f], 5) for f in FEATURES}

        # Univariate quintiles: predeclared, exhaustive, no threshold selection.
        for f in FEATURES:
            b = buckets[f]
            for q in range(5):
                indices = np.flatnonzero((b == q).fillna(False).to_numpy()).tolist()
                for h in HORIZONS:
                    st = stats_for(df, indices, h)
                    if st:
                        tests.append({"symbol": s, "kind": "univariate_quintile", "feature": f, "bucket": q, "horizon": h, **st})

        # Core 3-feature intersections using terciles, 27 combinations, all scanned.
        core = ["rvol26", "taker_buy_ratio", "rel_strength_6h"]
        terc = {f: qbucket(feats[f], 3) for f in core}
        for qr, qt, qs in product(range(3), repeat=3):
            mask = ((terc[core[0]] == qr) & (terc[core[1]] == qt) & (terc[core[2]] == qs)).fillna(False)
            indices = np.flatnonzero(mask.to_numpy()).tolist()
            for h in HORIZONS:
                st = stats_for(df, indices, h)
                if st:
                    tests.append({"symbol": s, "kind": "core_3way_tercile", "buckets": {"rvol26": qr, "taker_buy_ratio": qt, "rel_strength_6h": qs}, "horizon": h, **st})

    bh_adjust(tests)
    survivors = [r for r in tests if r["pass"]]
    survivors.sort(key=lambda r: (r["qValue"], -r["mean"], -r["n"]))
    report = {
        "engine": "SYSTEMATIC_EDGE_SCANNER_V1",
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "interval": INTERVAL,
        "days": DAYS,
        "featureDefinitionsFrozen": True,
        "bucketPolicy": {"univariate": "quintiles", "core3way": "terciles", "selection": "exhaustive predeclared"},
        "multipleTesting": {"method": "Benjamini-Hochberg", "q": FDR_Q, "family": "all symbol-horizon tests"},
        "rawGate": {"minEvents": MIN_EVENTS, "mean": 0.015, "median": ">0", "hitRate": ">0.55", "mfeMaeRatio": 2.0},
        "symbolsRequested": symbols,
        "symbolsLoaded": [s for s in symbols if s in data],
        "failures": failures,
        "tests": len(tests),
        "survivors": survivors,
        "survivorCount": len(survivors),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    print(json.dumps({"tests": len(tests), "survivors": len(survivors), "out": str(OUT)}, indent=2))


if __name__ == "__main__":
    main()
