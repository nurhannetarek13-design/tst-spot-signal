#!/usr/bin/env python3
"""Systematic edge discovery v3 — point-in-time / OOS / cross-symbol safe.

Research-only. This scanner deliberately does NOT authorize live trading.

Methodology changes vs v2:
- removes full-sample qcut thresholds (future leakage)
- uses point-in-time cross-sectional ranks only
- selects hypotheses on discovery data only, with BH-FDR
- freezes survivors before calibration and untouched test evaluation
- deterministic symbol holdout for cross-symbol validation
- time split for discovery/calibration/test
- baseline + stressed round-trip costs
- event de-clustering, path-aware MFE/MAE, bootstrap CI
- explicitly blocks promotion while historical/delisted-universe survivorship
  coverage is unresolved

The current-liquidity universe is acceptable for hypothesis DISCOVERY only. A
candidate cannot become live-eligible from this report, even if all statistical
checks pass.
"""
from __future__ import annotations

import hashlib
import json
import math
import pathlib
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np
import pandas as pd

BASE_URL = "https://data-api.binance.vision"
INTERVAL = "1h"
DAYS = 365
MAX_SYMBOLS = 80
MIN_QV = 10_000_000
DOWNLOAD_WORKERS = 8
API_RETRIES = 4
MIN_GAP_HOURS = 24
MIN_DISCOVERY_EVENTS = 50
MIN_VALIDATION_EVENTS = 35
FDR_Q = 0.05
HORIZONS = (24, 48, 72)
BASELINE_COST = 0.0028
STRESS_COST = 0.0050
SEVERE_COST = 0.0080
BOOTSTRAP_N = 500
SEED = 20260910
OUT = pathlib.Path("validation/edges/systematic-edge-scanner-v3-latest.json")
EXCLUDED = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "AEUR",
    "TRY", "BRL", "GBP", "AUD", "USD1", "RLUSD", "USDE", "PAXG", "XAUT",
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api(path: str):
    last = None
    for attempt in range(1, API_RETRIES + 1):
        try:
            req = urllib.request.Request(
                BASE_URL + path,
                headers={"User-Agent": "tst-systematic-edge-scanner-v3/1.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as exc:
            last = exc
            if attempt == API_RETRIES:
                break
            time.sleep(min(4.0, 0.5 * (2 ** (attempt - 1))))
    raise RuntimeError(f"API failed after {API_RETRIES} attempts: {path}: {last}")


def current_universe() -> list[str]:
    """Current liquid universe: discovery-only because it has survivorship bias."""
    info = api("/api/v3/exchangeInfo")
    ticker = {x["symbol"]: x for x in api("/api/v3/ticker/24hr")}
    rows: list[tuple[str, float]] = []
    for s in info.get("symbols", []):
        base = s.get("baseAsset", "")
        if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT":
            continue
        if not s.get("isSpotTradingAllowed"):
            continue
        if not base or base in EXCLUDED or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        qv = float(ticker.get(s["symbol"], {}).get("quoteVolume") or 0.0)
        if qv >= MIN_QV:
            rows.append((s["symbol"], qv))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:MAX_SYMBOLS]]


def klines(symbol: str) -> pd.DataFrame:
    end = int(time.time() * 1000)
    cur = end - DAYS * 86_400_000
    rows = []
    while cur < end:
        q = urllib.parse.urlencode(
            {"symbol": symbol, "interval": INTERVAL, "limit": 1000, "startTime": cur, "endTime": end}
        )
        batch = api("/api/v3/klines?" + q)
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1][0]) + 3_600_000
        if nxt <= cur:
            break
        cur = nxt
    if len(rows) < 2500:
        raise RuntimeError(f"{symbol}: insufficient bars {len(rows)}")
    df = pd.DataFrame(
        rows,
        columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
        ],
    )
    for c in ("open", "high", "low", "close", "volume", "quote_volume", "taker_quote"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df.open_time, unit="ms", utc=True)
    return df.set_index("ts")[["open", "high", "low", "close", "volume", "quote_volume", "taker_quote"]].dropna()


def load_history(symbols: list[str]):
    data: dict[str, pd.DataFrame] = {}
    fail: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as ex:
        futures = {ex.submit(klines, s): s for s in symbols}
        for n, fut in enumerate(as_completed(futures), 1):
            s = futures[fut]
            try:
                data[s] = fut.result()
                log(f"DATA {n}/{len(symbols)} {s}: {len(data[s])} bars")
            except Exception as exc:
                fail[s] = str(exc)
                log(f"DATA {n}/{len(symbols)} {s}: FAIL {exc}")
    return data, fail


def local_features(symbol: str, df: pd.DataFrame, btc: pd.Series) -> pd.DataFrame:
    c = df.close
    b = btc.reindex(df.index).ffill()
    r1 = c.pct_change()
    rv24 = r1.rolling(24, min_periods=20).std(ddof=0)
    rv168 = r1.rolling(168, min_periods=120).std(ddof=0)
    out = pd.DataFrame(index=df.index)
    out["symbol"] = symbol
    out["close"] = c
    out["high"] = df.high
    out["low"] = df.low
    out["ret_6h"] = c.pct_change(6)
    out["ret_24h"] = c.pct_change(24)
    out["btc_6h"] = b.pct_change(6)
    out["btc_24h"] = b.pct_change(24)
    out["rs_6h"] = out.ret_6h - out.btc_6h
    out["rs_24h"] = out.ret_24h - out.btc_24h
    out["accel"] = out.rs_6h - out.rs_24h / 4.0
    out["rvol26"] = df.quote_volume / df.quote_volume.rolling(26, min_periods=20).mean().replace(0, np.nan)
    out["taker_buy_ratio"] = df.taker_quote / df.quote_volume.replace(0, np.nan)
    out["vol_ratio"] = rv24 / rv168.replace(0, np.nan)
    out["drawdown_72h"] = c / c.rolling(72, min_periods=60).max() - 1.0
    out["rebound_6h"] = c / c.rolling(6, min_periods=6).min() - 1.0
    return out.replace([np.inf, -np.inf], np.nan)


def build_panel(data: dict[str, pd.DataFrame], symbols: list[str]) -> pd.DataFrame:
    btc = data["BTCUSDT"].close
    frames = [local_features(s, data[s], btc) for s in symbols if s in data]
    panel = pd.concat(frames).reset_index().rename(columns={"index": "ts"})
    # Point-in-time cross-sectional percentile ranks. No future observations are
    # used to decide the rank at timestamp t.
    for col in ("rs_6h", "rs_24h", "accel", "rvol26", "taker_buy_ratio", "vol_ratio", "drawdown_72h"):
        panel[f"cs_{col}"] = panel.groupby("ts", sort=False)[col].rank(pct=True, method="average")
    return panel.sort_values(["symbol", "ts"]).reset_index(drop=True)


def symbol_holdout(symbol: str) -> bool:
    # deterministic 20% holdout; stable across reruns and independent of returns
    v = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16) % 100
    return v < 20


def time_cuts(panel: pd.DataFrame):
    ts = np.array(sorted(panel.ts.dropna().unique()))
    if len(ts) < 1000:
        raise RuntimeError("insufficient panel timestamps")
    return ts[int(len(ts) * 0.50)], ts[int(len(ts) * 0.75)]


@dataclass(frozen=True)
class Hypothesis:
    family: str
    params: tuple

    @property
    def hid(self) -> str:
        payload = self.family + ":" + ",".join(map(str, self.params))
        return hashlib.sha1(payload.encode()).hexdigest()[:12]


def hypotheses() -> list[Hypothesis]:
    out: list[Hypothesis] = []
    # Strong relative-strength continuation with flow confirmation.
    for rs in (0.70, 0.80, 0.90):
        for taker in (0.54, 0.58, 0.62):
            for rvol in (1.00, 1.30, 1.60):
                out.append(Hypothesis("RS_FLOW", (rs, taker, rvol)))
    # Acceleration rather than absolute momentum.
    for accel in (0.70, 0.80, 0.90):
        for rs24 in (0.60, 0.75, 0.85):
            for rvol in (1.00, 1.30, 1.60):
                out.append(Hypothesis("RS_ACCEL", (accel, rs24, rvol)))
    # Volatility expansion while relative strength is already high.
    for vr in (1.10, 1.30, 1.60):
        for rs6 in (0.70, 0.80, 0.90):
            for taker in (0.54, 0.58, 0.62):
                out.append(Hypothesis("VOL_EXPANSION", (vr, rs6, taker)))
    # Deep drawdown followed by measurable recovery; not generic oversold buying.
    for dd in (-0.08, -0.12, -0.18):
        for rebound in (0.01, 0.02, 0.04):
            for taker in (0.50, 0.55, 0.60):
                out.append(Hypothesis("CRASH_RECOVERY", (dd, rebound, taker)))
    return out


def event_mask(df: pd.DataFrame, h: Hypothesis) -> pd.Series:
    if h.family == "RS_FLOW":
        rs, taker, rvol = h.params
        return (df.cs_rs_24h >= rs) & (df.taker_buy_ratio >= taker) & (df.rvol26 >= rvol)
    if h.family == "RS_ACCEL":
        accel, rs24, rvol = h.params
        return (df.cs_accel >= accel) & (df.cs_rs_24h >= rs24) & (df.rvol26 >= rvol) & (df.taker_buy_ratio >= 0.52)
    if h.family == "VOL_EXPANSION":
        vr, rs6, taker = h.params
        return (df.vol_ratio >= vr) & (df.cs_rs_6h >= rs6) & (df.taker_buy_ratio >= taker) & (df.rvol26 >= 1.10)
    if h.family == "CRASH_RECOVERY":
        dd, rebound, taker = h.params
        return (df.drawdown_72h <= dd) & (df.rebound_6h >= rebound) & (df.taker_buy_ratio >= taker)
    raise KeyError(h.family)


def decluster_rows(df: pd.DataFrame, mask: pd.Series) -> list[int]:
    idx: list[int] = []
    last_by_symbol: dict[str, pd.Timestamp] = {}
    for i in np.flatnonzero(mask.fillna(False).to_numpy()):
        s = str(df.symbol.iloc[i])
        ts = pd.Timestamp(df.ts.iloc[i])
        last = last_by_symbol.get(s)
        if last is None or (ts - last).total_seconds() >= MIN_GAP_HOURS * 3600:
            idx.append(i)
            last_by_symbol[s] = ts
    return idx


def event_paths(panel: pd.DataFrame, subset: pd.DataFrame, h: Hypothesis, horizon: int) -> pd.DataFrame:
    mask = event_mask(subset, h)
    rows = []
    for i in decluster_rows(subset, mask):
        r = subset.iloc[i]
        s = str(r.symbol)
        ts = pd.Timestamp(r.ts)
        sdf = panel[(panel.symbol == s) & (panel.ts >= ts)].sort_values("ts")
        if len(sdf) <= horizon:
            continue
        entry = float(sdf.close.iloc[0])
        future = sdf.iloc[1:horizon + 1]
        if entry <= 0 or len(future) < horizon:
            continue
        gross = float(sdf.close.iloc[horizon]) / entry - 1.0
        mfe = float(future.high.max()) / entry - 1.0
        mae = max(0.0, 1.0 - float(future.low.min()) / entry)
        rows.append({"symbol": s, "ts": ts, "gross": gross, "mfe": mfe, "mae": mae})
    return pd.DataFrame(rows)


def one_sided_pmean(a: np.ndarray) -> float:
    if len(a) < 2:
        return 1.0
    sd = float(np.std(a, ddof=1))
    if sd <= 0:
        return 0.0 if float(np.mean(a)) > 0 else 1.0
    z = float(np.mean(a)) / (sd / math.sqrt(len(a)))
    return 0.5 * math.erfc(z / math.sqrt(2))


def summarize(events: pd.DataFrame, cost: float = 0.0) -> dict:
    if events.empty:
        return {"n": 0}
    gross = events.gross.to_numpy(float)
    net = gross - cost
    mfe = events.mfe.to_numpy(float)
    mae = events.mae.to_numpy(float)
    med_mae = float(np.median(mae))
    ratio = float(np.median(mfe) / med_mae) if med_mae > 0 else (float("inf") if np.median(mfe) > 0 else 0.0)
    wins = net[net > 0]
    losses = -net[net < 0]
    pf = float(wins.sum() / losses.sum()) if losses.sum() > 0 else (float("inf") if wins.sum() > 0 else 0.0)
    return {
        "n": int(len(net)),
        "meanGross": float(np.mean(gross)),
        "meanNet": float(np.mean(net)),
        "medianNet": float(np.median(net)),
        "hitRateNet": float(np.mean(net > 0)),
        "medianMFE": float(np.median(mfe)),
        "medianMAE": med_mae,
        "mfeMaeRatio": ratio,
        "profitFactorNet": pf,
        "pMeanGrossPositive": one_sided_pmean(gross),
    }


def bootstrap_mean_ci(events: pd.DataFrame, cost: float, n: int = BOOTSTRAP_N) -> list[float] | None:
    if len(events) < 10:
        return None
    rng = np.random.default_rng(SEED + len(events))
    vals = events.gross.to_numpy(float) - cost
    means = np.empty(n, dtype=float)
    for i in range(n):
        means[i] = float(np.mean(rng.choice(vals, size=len(vals), replace=True)))
    return [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]


def bh(rows: list[dict]) -> None:
    ps = sorted([(i, float(r["discovery"]["pMeanGrossPositive"])) for i, r in enumerate(rows)], key=lambda x: x[1])
    m = len(ps)
    adj = [1.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i, p = ps[rank - 1]
        running = min(running, p * m / rank)
        adj[i] = min(1.0, running)
    for i, r in enumerate(rows):
        r["qValue"] = adj[i]


def discovery_gate(s: dict) -> bool:
    return bool(
        s.get("n", 0) >= MIN_DISCOVERY_EVENTS
        and s.get("meanGross", -9) >= 0.015
        and s.get("medianNet", -9) > 0
        and s.get("hitRateNet", 0) > 0.55
        and s.get("mfeMaeRatio", 0) >= 2.0
    )


def validation_gate(base: dict, stress: dict) -> bool:
    return bool(
        base.get("n", 0) >= MIN_VALIDATION_EVENTS
        and base.get("meanNet", -9) > 0
        and base.get("medianNet", -9) > 0
        and base.get("profitFactorNet", 0) > 1.05
        and stress.get("meanNet", -9) > 0
    )


def regime_label(btc: pd.DataFrame) -> pd.Series:
    c = btc.close
    r24 = c.pct_change(24)
    r168 = c.pct_change(168)
    vol24 = c.pct_change().rolling(24, min_periods=20).std(ddof=0)
    vol168 = c.pct_change().rolling(168, min_periods=120).std(ddof=0)
    vr = vol24 / vol168.replace(0, np.nan)
    out = pd.Series("SIDEWAYS", index=btc.index, dtype=object)
    out[(r168 > 0.05) & (r24 > 0)] = "STRONG_BULL"
    out[(r168 > 0) & ~(r168 > 0.05) & (r24 >= -0.02)] = "WEAK_BULL"
    out[(r168 < 0) & (r24 > -0.04)] = "WEAK_BEAR"
    out[(r24 <= -0.04) & (vr >= 1.20)] = "PANIC_HIGH_VOL_BEAR"
    recovery = (r24 > 0.02) & (c / c.rolling(72, min_periods=60).min() - 1.0 > 0.04) & (r168 < 0.02)
    out[recovery] = "POST_CRASH_RECOVERY"
    return out


def main() -> None:
    started = time.time()
    syms = current_universe()
    log(f"Universe selected={len(syms)} (CURRENT-LIQUIDITY DISCOVERY ONLY)")
    data, failures = load_history(sorted(set(["BTCUSDT"] + syms)))
    if "BTCUSDT" not in data:
        raise RuntimeError("BTC history unavailable")
    loaded = [s for s in syms if s in data]
    panel = build_panel(data, loaded)
    cut1, cut2 = time_cuts(panel)
    train_symbols = [s for s in loaded if not symbol_holdout(s)]
    holdout_symbols = [s for s in loaded if symbol_holdout(s)]
    log(f"Panel={len(panel)} rows trainSymbols={len(train_symbols)} holdoutSymbols={len(holdout_symbols)} cut1={cut1} cut2={cut2}")

    discovery = panel[(panel.ts < cut1) & panel.symbol.isin(train_symbols)].copy()
    calibration = panel[(panel.ts >= cut1) & (panel.ts < cut2) & panel.symbol.isin(train_symbols)].copy()
    temporal_test = panel[(panel.ts >= cut2) & panel.symbol.isin(train_symbols)].copy()
    strict_test = panel[(panel.ts >= cut2) & panel.symbol.isin(holdout_symbols)].copy()

    hyps = hypotheses()
    candidates: list[dict] = []
    for n, h in enumerate(hyps, 1):
        for horizon in HORIZONS:
            ev = event_paths(panel, discovery, h, horizon)
            d = summarize(ev, cost=0.0)
            candidates.append({
                "id": h.hid,
                "family": h.family,
                "params": list(h.params),
                "horizonHours": horizon,
                "discovery": d,
                "rawDiscoveryPass": discovery_gate(d),
            })
        if n % 25 == 0 or n == len(hyps):
            log(f"DISCOVERY {n}/{len(hyps)} hypotheses; tests={len(candidates)}")

    bh(candidates)
    survivors = [r for r in candidates if r["rawDiscoveryPass"] and r["qValue"] <= FDR_Q]
    log(f"Discovery BH survivors={len(survivors)}/{len(candidates)}")

    btc_regime = regime_label(data["BTCUSDT"])
    final = []
    for n, r in enumerate(survivors, 1):
        h = Hypothesis(r["family"], tuple(r["params"]))
        horizon = int(r["horizonHours"])
        cal_ev = event_paths(panel, calibration, h, horizon)
        t_ev = event_paths(panel, temporal_test, h, horizon)
        s_ev = event_paths(panel, strict_test, h, horizon)
        cal_base = summarize(cal_ev, BASELINE_COST)
        cal_stress = summarize(cal_ev, STRESS_COST)
        test_base = summarize(t_ev, BASELINE_COST)
        test_stress = summarize(t_ev, STRESS_COST)
        test_severe = summarize(t_ev, SEVERE_COST)
        strict_base = summarize(s_ev, BASELINE_COST)
        strict_stress = summarize(s_ev, STRESS_COST)
        # Regime reporting on temporal test. This is diagnostic only and does not
        # change hypothesis parameters.
        regime_stats = {}
        if not t_ev.empty:
            tmp = t_ev.copy()
            tmp["regime"] = [btc_regime.reindex([pd.Timestamp(x)]).iloc[0] if pd.Timestamp(x) in btc_regime.index else "UNKNOWN" for x in tmp.ts]
            for regime, grp in tmp.groupby("regime"):
                regime_stats[str(regime)] = summarize(grp, BASELINE_COST)
        row = dict(r)
        row.update({
            "calibrationBaseline": cal_base,
            "calibrationStress": cal_stress,
            "temporalTestBaseline": test_base,
            "temporalTestStress": test_stress,
            "temporalTestSevere": test_severe,
            "strictTimeAndSymbolHoldoutBaseline": strict_base,
            "strictTimeAndSymbolHoldoutStress": strict_stress,
            "temporalTestBootstrap95MeanNet": bootstrap_mean_ci(t_ev, BASELINE_COST),
            "strictTestBootstrap95MeanNet": bootstrap_mean_ci(s_ev, BASELINE_COST),
            "regimeDiagnostics": regime_stats,
            "calibrationPass": validation_gate(cal_base, cal_stress),
            "temporalTestPass": validation_gate(test_base, test_stress),
            "strictTimeAndSymbolHoldoutPass": validation_gate(strict_base, strict_stress),
        })
        row["statisticalPass"] = bool(row["calibrationPass"] and row["temporalTestPass"] and row["strictTimeAndSymbolHoldoutPass"])
        # Hard methodology block: current-universe survivorship has not yet been
        # eliminated, so no candidate can be promoted from this report.
        row["promotionAllowed"] = False
        row["promotionBlock"] = "HISTORICAL_DELISTED_UNIVERSE_NOT_YET_VALIDATED"
        final.append(row)
        log(f"VALIDATE {n}/{len(survivors)} {h.family} h={horizon} statisticalPass={row['statisticalPass']}")

    report = {
        "engine": "SYSTEMATIC_EDGE_SCANNER_V3",
        "engineRevision": "1.0-point-in-time-oos",
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "promotionAllowed": False,
        "methodology": {
            "futureLeakageFix": "NO_FULL_SAMPLE_QUANTILES; POINT_IN_TIME_CROSS_SECTIONAL_RANKS",
            "selection": "DISCOVERY_ONLY",
            "multipleTesting": {"method": "Benjamini-Hochberg", "q": FDR_Q, "appliedTo": "discovery hypotheses only"},
            "timeSplit": {"discovery": "first 50%", "calibration": "next 25%", "test": "last 25%"},
            "symbolHoldout": "deterministic 20% SHA256 holdout",
            "declusterHours": MIN_GAP_HOURS,
            "baselineRoundTripCost": BASELINE_COST,
            "stressRoundTripCost": STRESS_COST,
            "severeRoundTripCost": SEVERE_COST,
            "bootstrapSamples": BOOTSTRAP_N,
            "survivorshipBias": "UNRESOLVED_CURRENT_LIQUIDITY_UNIVERSE",
            "survivorshipConsequence": "NO_LIVE_PROMOTION_FROM_THIS_REPORT",
        },
        "rawDiscoveryGate": {
            "minEvents": MIN_DISCOVERY_EVENTS,
            "meanGross": 0.015,
            "medianGross": ">0",
            "hitRateGross": ">0.55",
            "mfeMaeRatio": 2.0,
        },
        "validationGate": {
            "minEvents": MIN_VALIDATION_EVENTS,
            "baselineMeanNet": ">0",
            "baselineMedianNet": ">0",
            "baselineProfitFactor": ">1.05",
            "stressMeanNet": ">0",
        },
        "symbolsRequested": syms,
        "symbolsLoaded": loaded,
        "trainSymbols": train_symbols,
        "holdoutSymbols": holdout_symbols,
        "failures": failures,
        "cut1": str(cut1),
        "cut2": str(cut2),
        "hypotheses": len(hyps),
        "discoveryTests": len(candidates),
        "discoverySurvivorCount": len(survivors),
        "statisticalPassCount": sum(1 for x in final if x.get("statisticalPass")),
        "liveEligibleCount": 0,
        "results": final,
        "runtimeSeconds": round(time.time() - started, 2),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    log(f"DONE survivors={len(survivors)} statisticalPass={report['statisticalPassCount']} liveEligible=0 out={OUT}")


if __name__ == "__main__":
    main()
