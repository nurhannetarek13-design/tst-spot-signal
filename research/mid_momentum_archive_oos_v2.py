#!/usr/bin/env python3
"""Research-only archived-symbol OOS test for MID_MOMENTUM_CONTINUATION_V1.

The nine setup/TP-SL hypotheses are frozen from V1. This run evaluates them on
41 USDT Spot symbols that had Binance Vision 15m archives in Mar-Aug 2026 but
are absent from the current trading universe. Current liquid symbols are used
only as a cross-sectional reference panel; scored trades are archived-only.

No production promotion is possible from this run.
"""
from __future__ import annotations

import io
import json
import math
import pathlib
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from research import intraday_barrier_edge_scanner_v1 as base
from research import mid_momentum_continuation_v1 as mid

CDN = "https://data.binance.vision"
MONTHS = ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
INTERVAL = "15m"
REFERENCE_CURRENT = 50
WORKERS = 24
MIN_BARS = 120
MIN_OOS = 30
FDR_Q = 0.10
OUT = pathlib.Path("validation/edges/mid-momentum-archive-oos-v2-latest.json")
UA = "tst-mid-momentum-archive-oos-v2/1.0"

ARCHIVED_ONLY_TARGETS = [
    "ACXUSDT","HFTUSDT","ICXUSDT","PIVXUSDT","PYRUSDT","SCRTUSDT","STORJUSDT","VANRYUSDT","VICUSDT",
    "ALCXUSDT","ARDRUSDT","NFPUSDT","PONDUSDT","COSUSDT","DUSDT","HIGHUSDT","MBOXUSDT","TONUSDT",
    "ATAUSDT","FARMUSDT","MLNUSDT","PHBUSDT","SYSUSDT","A2ZUSDT","BIFIUSDT","DEGOUSDT","DENTUSDT",
    "FIOUSDT","FORTHUSDT","FUNUSDT","HOOKUSDT","IDEXUSDT","LRCUSDT","MDTUSDT","NTRNUSDT","OXTUSDT",
    "RDNTUSDT","SXPUSDT","TRUUSDT","UTKUSDT","WANUSDT",
]

COLS = ["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def month_url(symbol: str, month: str) -> str:
    return f"{CDN}/data/spot/monthly/klines/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{month}.zip"


def read_month(symbol: str, month: str) -> pd.DataFrame | None:
    req = urllib.request.Request(month_url(symbol, month), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise RuntimeError(f"{symbol} {month}: no csv in archive")
        with zf.open(names[0]) as fh:
            df = pd.read_csv(fh, header=None, names=COLS)
    if df.empty:
        return None
    ts = pd.to_numeric(df.open_time, errors="coerce")
    ts = np.where(ts > 100_000_000_000_000, np.floor(ts / 1000), ts)
    out = pd.DataFrame({
        "ts": pd.to_datetime(ts, unit="ms", utc=True, errors="coerce"),
        "open": pd.to_numeric(df.open, errors="coerce"),
        "high": pd.to_numeric(df.high, errors="coerce"),
        "low": pd.to_numeric(df.low, errors="coerce"),
        "close": pd.to_numeric(df.close, errors="coerce"),
        "quote_volume": pd.to_numeric(df.quote_volume, errors="coerce"),
        "taker_quote": pd.to_numeric(df.taker_quote, errors="coerce"),
    }).dropna()
    return out


def load_symbol(symbol: str) -> tuple[str, pd.DataFrame | None, list[str]]:
    parts = []
    present = []
    for month in MONTHS:
        z = read_month(symbol, month)
        if z is not None and not z.empty:
            parts.append(z)
            present.append(month)
    if not parts:
        return symbol, None, present
    df = pd.concat(parts, ignore_index=True).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return symbol, df, present


def contiguous_segments(df: pd.DataFrame) -> list[pd.DataFrame]:
    if df.empty:
        return []
    x = df.sort_values("ts").reset_index(drop=True)
    gap = x.ts.diff().dt.total_seconds().fillna(900).ne(900)
    gid = gap.cumsum()
    return [g.reset_index(drop=True) for _, g in x.groupby(gid) if len(g) >= MIN_BARS]


def enrich_symbol(df: pd.DataFrame, btc: pd.DataFrame, symbol: str) -> list[pd.DataFrame]:
    frames = []
    for seg in contiguous_segments(df):
        z = mid.augment(seg, btc)
        z["symbol"] = symbol
        frames.append(z)
    return frames


def barrier_contiguous(raw: pd.DataFrame, idx: int, c: dict, event_ts, regime: str):
    if idx + 1 >= len(raw):
        return None
    entry_ts = pd.Timestamp(raw.ts.iloc[idx + 1])
    if entry_ts - pd.Timestamp(raw.ts.iloc[idx]) != pd.Timedelta(minutes=15):
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
    prev_ts = pd.Timestamp(raw.ts.iloc[idx])
    last_j = idx
    for j in range(idx + 1, end + 1):
        ts = pd.Timestamp(raw.ts.iloc[j])
        if ts - prev_ts != pd.Timedelta(minutes=15):
            break
        prev_ts = ts
        last_j = j
        hi = float(raw.high.iloc[j]); lo = float(raw.low.iloc[j])
        mfe = max(mfe, hi / entry - 1.0); mae = max(mae, 1.0 - lo / entry)
        hit_tp = hi >= tp; hit_sl = lo <= sl
        if hit_tp and hit_sl:
            gross = -c["sl"]; outcome = "SL_AMBIGUOUS"; break
        if hit_sl:
            gross = -c["sl"]; outcome = "SL"; break
        if hit_tp:
            gross = c["tp"]; outcome = "TP"; break
    if last_j == idx:
        return None
    if gross is None:
        gross = max(-c["sl"], min(c["tp"], float(raw.close.iloc[last_j]) / entry - 1.0))
    return {
        "gross": gross, "outcome": outcome, "mfe": mfe, "mae": mae,
        "day": pd.Timestamp(event_ts).strftime("%Y-%m-%d"), "regime": regime,
    }


def symbol_dispersion(rows_by_symbol: dict[str, list[dict]], cost: float) -> dict:
    vals = []
    for s, rows in rows_by_symbol.items():
        if rows:
            vals.append((s, float(np.mean([r["gross"] - cost for r in rows])), len(rows)))
    if not vals:
        return {"symbols": 0}
    means = np.asarray([v[1] for v in vals])
    return {
        "symbols": len(vals),
        "positiveSymbolShare": float(np.mean(means > 0)),
        "medianSymbolMeanNet": float(np.median(means)),
        "best": sorted(({"symbol": s, "meanNet": m, "n": n} for s,m,n in vals), key=lambda x: x["meanNet"], reverse=True)[:5],
        "worst": sorted(({"symbol": s, "meanNet": m, "n": n} for s,m,n in vals), key=lambda x: x["meanNet"])[:5],
    }


def bh(rows: list[dict]) -> None:
    order = sorted(range(len(rows)), key=lambda i: rows[i]["pMeanNetPositive"])
    m = len(order); adjusted = [1.0] * len(rows); running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, rows[i]["pMeanNetPositive"] * m / rank)
        adjusted[i] = min(1.0, running)
    for i, row in enumerate(rows):
        row["qValue"] = adjusted[i]


def main() -> None:
    started = time.time()
    # Reference symbols are frozen at run start by current liquidity ranking;
    # target trades remain archived-only and cannot leak into the reference set.
    reference = [s for s in base.universe() if s not in ARCHIVED_ONLY_TARGETS][:REFERENCE_CURRENT]
    requested = sorted(set(reference + ARCHIVED_ONLY_TARGETS + ["BTCUSDT"]))
    data = {}; months_present = {}; failures = {}
    log(f"targets={len(ARCHIVED_ONLY_TARGETS)} reference={len(reference)} requested={len(requested)}")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        fut = {ex.submit(load_symbol, s): s for s in requested}
        for n, f in enumerate(as_completed(fut), 1):
            s = fut[f]
            try:
                sym, df, months = f.result(); months_present[sym] = months
                if df is not None and len(df) >= MIN_BARS:
                    data[sym] = df
                log(f"data {n}/{len(requested)} {s} bars={0 if df is None else len(df)} months={len(months)}")
            except Exception as exc:
                failures[s] = f"{type(exc).__name__}:{exc}"; log(f"data {n}/{len(requested)} {s} FAIL {failures[s]}")
    if "BTCUSDT" not in data:
        raise RuntimeError("BTCUSDT archive history unavailable")

    target_loaded = [s for s in ARCHIVED_ONLY_TARGETS if s in data]
    reference_loaded = [s for s in reference if s in data]
    frames = []
    for s in reference_loaded + target_loaded:
        frames.extend(enrich_symbol(data[s], data["BTCUSDT"], s))
    if not frames:
        raise RuntimeError("no archive panel")
    panel = mid.cross_sectional(pd.concat(frames, ignore_index=True))
    panel = panel.dropna(subset=["ret_15m","ret_1h","ret_4h","ret_24h","qv24","volume_ratio","taker_buy_ratio","rsi14","btc_ret_4h","btc_ret_24h"])
    panel["regime"] = np.select(
        [panel.btc_ret_4h > 0.01, panel.btc_ret_4h < -0.01, panel.breadth_4h >= 0.60, panel.breadth_4h <= 0.40],
        ["BTC_UP","BTC_DOWN","BROAD_UP","BROAD_DOWN"], default="MIXED",
    )

    raw = {s: data[s].sort_values("ts").reset_index(drop=True) for s in target_loaded}
    lookup = {s: {pd.Timestamp(t): i for i,t in enumerate(df.ts)} for s,df in raw.items()}
    target_mask = panel.symbol.isin(target_loaded)
    results = []
    for family, mask in mid.masks(panel).items():
        selected = mid.event_rows(panel, mask & target_mask)
        for cname, contract in mid.CONTRACTS.items():
            rows = []; by_symbol = {}
            for r in selected.itertuples(index=False):
                idx = lookup[r.symbol].get(pd.Timestamp(r.ts))
                if idx is None: continue
                z = barrier_contiguous(raw[r.symbol], idx, contract, r.ts, r.regime)
                if z:
                    rows.append(z); by_symbol.setdefault(r.symbol, []).append(z)
            normal = mid.summarize(rows, mid.COST_NORMAL)
            stress = mid.summarize(rows, mid.COST_STRESS)
            boot = mid.bootstrap(rows, mid.COST_NORMAL)
            results.append({
                "family": family, "contract": cname,
                "normal": normal, "stress": stress,
                "bootstrap": boot,
                "pMeanNetPositive": mid.pmean(rows, mid.COST_NORMAL),
                "symbolDispersion": symbol_dispersion(by_symbol, mid.COST_NORMAL),
                "regimes": {k: mid.summarize([x for x in rows if x["regime"] == k], mid.COST_NORMAL) for k in sorted({x["regime"] for x in rows})},
            })
    bh(results)
    for r in results:
        r["archivedOosPass"] = bool(
            r["normal"].get("n",0) >= MIN_OOS
            and r["normal"].get("meanNet",-1) > 0
            and r["normal"].get("profitFactor",0) >= 1.05
            and r["stress"].get("meanNet",-1) > 0
            and r["bootstrap"].get("status") == "OK"
            and r["bootstrap"].get("p05",-1) > 0
            and r["qValue"] <= FDR_Q
        )
        r["productionEligible"] = False

    passes = [r for r in results if r["archivedOosPass"]]
    payload = {
        "engine": "MID_MOMENTUM_ARCHIVE_OOS_V2",
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "automaticPromotion": False,
        "productionPromotionBlocked": True,
        "frozenFrom": "MID_MOMENTUM_CONTINUATION_V1",
        "archiveMonths": MONTHS,
        "targetDefinition": "USDT Spot symbols with >=2 monthly 15m archives in Mar-Aug 2026 and absent from current trading universe at audit time",
        "targetsRequested": ARCHIVED_ONLY_TARGETS,
        "targetsLoaded": target_loaded,
        "referenceCurrentRequested": reference,
        "referenceCurrentLoaded": reference_loaded,
        "monthsPresent": months_present,
        "failures": failures,
        "validationDesign": {
            "scoredUniverse": "ARCHIVED_ONLY_TARGETS",
            "crossSectionalReference": "archive bars for current liquid reference + archived-only targets",
            "entryTiming": "NEXT_15M_OPEN",
            "sameBarTpSlResolution": "SL_FIRST_PESSIMISTIC",
            "gapPolicy": "NO_BARRIER_TRAVERSAL_ACROSS_MISSING_15M_BARS",
            "costNormal": mid.COST_NORMAL,
            "costStress": mid.COST_STRESS,
            "BH_FDR_q": FDR_Q,
            "bootstrap": "day-cluster",
        },
        "hypothesisCount": len(results),
        "archivedOosPassCount": len(passes),
        "passes": passes,
        "rankedDiagnostics": sorted(results, key=lambda r: r["normal"].get("meanNet",-99), reverse=True),
        "productionBlockers": ["V1_DISCOVERY_DID_NOT_PASS", "WALK_FORWARD_NOT_ESTABLISHED"],
        "runtimeSeconds": round(time.time() - started, 2),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    log(f"DONE hypotheses={len(results)} archivedOosPass={len(passes)} targetsLoaded={len(target_loaded)}")


if __name__ == "__main__":
    main()
