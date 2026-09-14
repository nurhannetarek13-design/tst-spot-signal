#!/usr/bin/env python3
"""Run AdaptiveGridSafeSpotV1 on official Binance Vision Spot monthly 15m archives.

Research only: no account credentials, no order endpoints, no live trading.
Missing archive months are recorded; symbols with <90% expected coverage fail closed
without preventing the remaining validation universe from running.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from adaptive_grid_safe_v1 import Params, metrics, run_symbol

AUTHORIZATION = "RESEARCH_ONLY"
BASE = "https://data.binance.vision/data/spot/monthly/klines"
UA = "tst-adaptive-grid-safe-v1/1.1"
KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
    "trades", "taker_base", "taker_quote", "ignore",
]


def http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def fetch_verified_zip(url: str) -> bytes:
    raw = http_bytes(url)
    expected = http_bytes(url + ".CHECKSUM").decode(errors="replace").strip().split()[0].lower()
    actual = hashlib.sha256(raw).hexdigest()
    if len(expected) != 64 or actual != expected:
        raise RuntimeError(f"checksum mismatch for {url}")
    return raw


def parse_epoch(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    med = float(numeric.dropna().median()) if numeric.notna().any() else 0.0
    unit = "ns" if med > 1e17 else ("us" if med > 1e14 else "ms")
    return pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")


def read_kline_zip(raw: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        csvs = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(csvs) != 1:
            raise RuntimeError(f"expected one CSV, got {csvs}")
        with zf.open(csvs[0]) as f:
            df = pd.read_csv(f, header=None)
    if len(df) and not str(df.iloc[0, 0]).replace("-", "").isdigit():
        df = df.iloc[1:].reset_index(drop=True)
    if df.shape[1] < len(KLINE_COLS):
        raise RuntimeError(f"unexpected kline columns: {df.shape[1]}")
    df = df.iloc[:, : len(KLINE_COLS)].copy()
    df.columns = KLINE_COLS
    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = parse_epoch(df["open_time"])
    return df.dropna(subset=["ts", "open", "high", "low", "close"])


def month_stamps(start: str, end: str) -> list[str]:
    a = pd.Timestamp(start).to_period("M")
    b = (pd.Timestamp(end) - pd.Timedelta(microseconds=1)).to_period("M")
    return [str(p) for p in pd.period_range(a, b, freq="M")]


def fetch_vision_klines(symbol: str, start: str, end: str) -> tuple[pd.DataFrame, list[dict], float]:
    parts = []
    sources = []
    for stamp in month_stamps(start, end):
        url = f"{BASE}/{symbol}/15m/{symbol}-15m-{stamp}.zip"
        try:
            raw = fetch_verified_zip(url)
            df = read_kline_zip(raw)
            parts.append(df)
            sources.append({"month": stamp, "url": url, "status": "OK", "rows": len(df), "checksumVerified": True})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                sources.append({"month": stamp, "url": url, "status": "MISSING_404", "rows": 0, "checksumVerified": False})
                continue
            raise

    if not parts:
        raise RuntimeError(f"No Binance Vision monthly data for {symbol}")

    df = pd.concat(parts, ignore_index=True).sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    df = df[(df["ts"] >= start_ts) & (df["ts"] < end_ts)].copy().reset_index(drop=True)
    expected = max(1, int((end_ts - start_ts) / pd.Timedelta(minutes=15)))
    coverage = len(df) / expected
    if coverage < 0.90:
        missing = [s["month"] for s in sources if s["status"] != "OK"]
        raise RuntimeError(f"Insufficient data coverage for {symbol}: {coverage:.3%}; missingMonths={missing}")

    df["open_time"] = (df["ts"].astype("int64") // 1_000_000).astype("int64")
    return df, sources, coverage


def finite_metrics(ts):
    m = metrics(ts)
    for k, v in list(m.items()):
        if isinstance(v, float) and not math.isfinite(v):
            m[k] = 999.0 if k == "profit_factor" and v > 0 else 0.0
    return m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["LINKUSDT", "TONUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--start", default="2025-09-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--oos-fraction", type=float, default=0.30)
    ap.add_argument("--output", default="artifacts/adaptive-grid-safe-v1/report.json")
    args = ap.parse_args()

    p = Params()
    report = {
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "source": "Binance Public Data / data.binance.vision Spot monthly klines",
        "checksumVerification": True,
        "strategy": "AdaptiveGridSafeSpotV1",
        "timeframe": "15m",
        "params": asdict(p),
        "window": {"start": args.start, "end": args.end, "oos_fraction": args.oos_fraction},
        "symbols": {},
    }
    all_trades = []

    for symbol in args.symbols:
        print(f"Loading Binance Vision {symbol}...")
        try:
            df, sources, coverage = fetch_vision_klines(symbol, args.start, args.end)
            trades, meta = run_symbol(symbol, df, p, args.oos_fraction)
            all_trades.extend(trades)
            is_trades = [t for t in trades if t.segment == "IS"]
            oos_trades = [t for t in trades if t.segment == "OOS"]
            report["symbols"][symbol] = {
                "status": "TESTED",
                "dataCoverage": coverage,
                "meta": meta,
                "sources": sources,
                "overall": finite_metrics(trades),
                "IS": finite_metrics(is_trades),
                "OOS": finite_metrics(oos_trades),
                "trades_detail": [asdict(t) for t in trades],
            }
            print(symbol, json.dumps(report["symbols"][symbol]["OOS"], indent=2))
        except Exception as e:
            report["symbols"][symbol] = {
                "status": "DATA_UNAVAILABLE",
                "error": f"{type(e).__name__}: {e}",
            }
            print(symbol, report["symbols"][symbol]["error"])

    report["aggregate"] = {
        "overall": finite_metrics(all_trades),
        "IS": finite_metrics([t for t in all_trades if t.segment == "IS"]),
        "OOS": finite_metrics([t for t in all_trades if t.segment == "OOS"]),
    }

    eligible = []
    for symbol, r in report["symbols"].items():
        if r.get("status") != "TESTED":
            continue
        o = r["OOS"]
        if (
            o["trades"] >= 20
            and o["profit_factor"] >= 1.20
            and o["avg_trade_pct"] > 0
            and o["max_drawdown_pct"] <= 2.0
        ):
            eligible.append(symbol)

    report["promotionGate"] = {
        "decision": "REJECT" if len(eligible) < 2 else "RESEARCH_PASS_NOT_LIVE",
        "eligibleSymbols": eligible,
        "requiresAtLeastTwoSymbols": True,
        "canEnableLiveTrading": False,
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({
        "aggregate": report["aggregate"],
        "promotionGate": report["promotionGate"],
        "symbolStatus": {k: v.get("status") for k, v in report["symbols"].items()},
    }, indent=2))


if __name__ == "__main__":
    main()
