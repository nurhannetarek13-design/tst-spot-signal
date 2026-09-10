#!/usr/bin/env python3
"""Research-only historical Binance USD-M flow feature layer.

Sources (Binance Public Data / data.binance.vision):
- aggTrades: aggressive buy/sell flow, delta, CVD, trade intensity
- markPriceKlines: mark price OHLC
- indexPriceKlines: index price OHLC
- premiumPriceKlines: premium/basis OHLC

Important: aggTrades do NOT contain order-book state, so this module never
labels any trade-derived feature as microprice or order-book imbalance.
Those features belong to the live/forward depth collector.

No account endpoints, order endpoints, API keys, or live trading.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import pathlib
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

AUTHORIZATION = "RESEARCH_ONLY"
BASE = "https://data.binance.vision/data/futures/um"
UA = "tst-binance-vision-historical-feature-layer/1.0"

AGG_COLS = [
    "aggregate_trade_id", "price", "quantity", "first_trade_id",
    "last_trade_id", "timestamp", "is_buyer_maker",
]
KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    interval: str | None = None

    def relative_dir(self, cadence: str, symbol: str) -> str:
        if self.interval:
            return f"{cadence}/{self.name}/{symbol}/{self.interval}"
        return f"{cadence}/{self.name}/{symbol}"

    def filename(self, symbol: str, stamp: str) -> str:
        if self.interval:
            return f"{symbol}-{self.interval}-{stamp}.zip"
        return f"{symbol}-{self.name}-{stamp}.zip"


DATASETS = {
    "aggTrades": DatasetSpec("aggTrades"),
    "markPriceKlines": DatasetSpec("markPriceKlines", "15m"),
    "indexPriceKlines": DatasetSpec("indexPriceKlines", "15m"),
    "premiumPriceKlines": DatasetSpec("premiumPriceKlines", "15m"),
}


def http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fetch_verified_zip(url: str, verify_checksum: bool = True) -> bytes:
    raw = http_bytes(url)
    if not verify_checksum:
        return raw
    checksum_raw = http_bytes(url + ".CHECKSUM").decode("utf-8", errors="replace").strip()
    expected = checksum_raw.split()[0].lower() if checksum_raw else ""
    actual = sha256_bytes(raw)
    if len(expected) != 64 or actual != expected:
        raise RuntimeError(f"checksum mismatch: expected={expected!r} actual={actual}")
    return raw


def read_zip_csv(raw: bytes, names: list[str]) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise RuntimeError(f"expected exactly one CSV in archive, got {csv_names}")
        with zf.open(csv_names[0]) as f:
            # Binance archives are not perfectly uniform about headers across eras.
            probe = pd.read_csv(f, nrows=3, header=None)
        has_header = False
        if len(probe):
            first = str(probe.iloc[0, 0]).strip().lower()
            has_header = any(token in first for token in ("id", "open", "timestamp", "time")) and not first.replace("-", "").isdigit()
        with zf.open(csv_names[0]) as f:
            df = pd.read_csv(f, header=0 if has_header else None)
    if df.shape[1] < len(names):
        raise RuntimeError(f"unexpected column count {df.shape[1]} < {len(names)}")
    df = df.iloc[:, :len(names)].copy()
    df.columns = names
    return df


def bool_series(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return s
    return s.astype(str).str.strip().str.lower().isin({"true", "1", "t"})


def parse_ms_timestamp(s: pd.Series) -> pd.DatetimeIndex:
    # USD-M futures archive timestamps are milliseconds in Binance's documented
    # futures schema. Guard against accidental micro/nano values anyway.
    x = pd.to_numeric(s, errors="coerce")
    med = float(x.dropna().median()) if x.notna().any() else 0.0
    if med > 1e17:
        unit = "ns"
    elif med > 1e14:
        unit = "us"
    else:
        unit = "ms"
    return pd.to_datetime(x, unit=unit, utc=True, errors="coerce")


def process_agg(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    for c in ("price", "quantity"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["timestamp", "price", "quantity"])
    df["timestamp"] = parse_ms_timestamp(df["timestamp"])
    df = df.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()
    maker = bool_series(df["is_buyer_maker"])
    df["buy_base"] = np.where(~maker, df["quantity"], 0.0)
    df["sell_base"] = np.where(maker, df["quantity"], 0.0)
    df["buy_quote"] = df["buy_base"] * df["price"]
    df["sell_quote"] = df["sell_base"] * df["price"]
    df["trade_quote"] = df["quantity"] * df["price"]
    df["signed_quote"] = df["buy_quote"] - df["sell_quote"]

    g = df.resample(freq, label="left", closed="left").agg(
        trade_open=("price", "first"),
        trade_high=("price", "max"),
        trade_low=("price", "min"),
        trade_close=("price", "last"),
        buy_base=("buy_base", "sum"),
        sell_base=("sell_base", "sum"),
        buy_quote=("buy_quote", "sum"),
        sell_quote=("sell_quote", "sum"),
        total_quote=("trade_quote", "sum"),
        delta_quote=("signed_quote", "sum"),
        agg_trade_count=("price", "size"),
        avg_trade_quote=("trade_quote", "mean"),
    )
    g["buy_share_quote"] = g["buy_quote"] / g["total_quote"].replace(0, np.nan)
    g["taker_buy_sell_ratio_quote"] = g["buy_quote"] / g["sell_quote"].replace(0, np.nan)
    g["cvd_quote"] = g["delta_quote"].cumsum()
    g["flow_imbalance_quote"] = g["delta_quote"] / g["total_quote"].replace(0, np.nan)
    return g


def process_kline(df: pd.DataFrame, prefix: str, freq: str) -> pd.DataFrame:
    for c in ("open", "high", "low", "close", "volume", "quote_volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = parse_ms_timestamp(df["open_time"])
    df = df.dropna(subset=["open_time"]).set_index("open_time").sort_index()
    # Archives requested at 15m can be resampled upward if the target frequency is larger.
    out = df[["open", "high", "low", "close", "volume", "quote_volume"]].resample(freq, label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum", "quote_volume": "sum",
    })
    out.columns = [f"{prefix}_{c}" for c in out.columns]
    return out


def daily_url(spec: DatasetSpec, symbol: str, d: date) -> str:
    stamp = d.isoformat()
    return f"{BASE}/{spec.relative_dir('daily', symbol)}/{spec.filename(symbol, stamp)}"


def monthly_url(spec: DatasetSpec, symbol: str, month: str) -> str:
    return f"{BASE}/{spec.relative_dir('monthly', symbol)}/{spec.filename(symbol, month)}"


def iter_days(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def load_dataset_daily(spec: DatasetSpec, symbol: str, start: date, end: date, checksum: bool) -> tuple[pd.DataFrame, list[dict]]:
    parts: list[pd.DataFrame] = []
    failures: list[dict] = []
    names = AGG_COLS if spec.name == "aggTrades" else KLINE_COLS
    for d in iter_days(start, end):
        url = daily_url(spec, symbol, d)
        try:
            raw = fetch_verified_zip(url, checksum)
            parts.append(read_zip_csv(raw, names))
        except urllib.error.HTTPError as exc:
            failures.append({"dataset": spec.name, "date": d.isoformat(), "status": exc.code, "url": url})
        except Exception as exc:
            failures.append({"dataset": spec.name, "date": d.isoformat(), "error": f"{type(exc).__name__}: {exc}", "url": url})
    return (pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=names)), failures


def build(symbol: str, start: date, end: date, freq: str, checksum: bool) -> tuple[pd.DataFrame, dict]:
    loaded: dict[str, int] = {}
    failures: list[dict] = []

    agg_raw, bad = load_dataset_daily(DATASETS["aggTrades"], symbol, start, end, checksum)
    failures.extend(bad)
    loaded["aggTrades"] = len(agg_raw)
    if agg_raw.empty:
        raise RuntimeError("no aggTrades loaded; cannot build historical flow layer")
    frame = process_agg(agg_raw, freq)

    for name, prefix in (("markPriceKlines", "mark"), ("indexPriceKlines", "index"), ("premiumPriceKlines", "premium")):
        raw, bad = load_dataset_daily(DATASETS[name], symbol, start, end, checksum)
        failures.extend(bad)
        loaded[name] = len(raw)
        if not raw.empty:
            frame = frame.join(process_kline(raw, prefix, freq), how="left")

    if {"mark_close", "index_close"}.issubset(frame.columns):
        frame["mark_index_basis_bps"] = (frame["mark_close"] / frame["index_close"] - 1.0) * 10000.0
    if "trade_close" in frame.columns:
        frame["ret_1bar"] = frame["trade_close"].pct_change()
        frame["realized_range_bps"] = (frame["trade_high"] / frame["trade_low"] - 1.0) * 10000.0

    frame.insert(0, "symbol", symbol)
    frame.index.name = "ts"
    frame = frame.reset_index()
    meta = {
        "engine": "BINANCE_VISION_HISTORICAL_FEATURE_LAYER_V1",
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "source": "Binance Public Data / data.binance.vision",
        "symbol": symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "frequency": freq,
        "checksumVerification": checksum,
        "rows": len(frame),
        "rawRows": loaded,
        "failures": failures,
        "featureSemantics": {
            "tradeFlow": "derived from aggTrades aggressor side using is_buyer_maker",
            "cvd": "cumulative signed quote-volume within the loaded sample",
            "microprice": "NOT AVAILABLE historically from aggTrades; requires order-book depth",
            "orderBookImbalance": "NOT AVAILABLE historically from aggTrades; requires order-book depth",
        },
    }
    return frame, meta


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--freq", default="5min", choices=["1min", "5min", "15min", "1h"])
    p.add_argument("--output-dir", default="artifacts/binance-vision-historical-features")
    p.add_argument("--no-checksum", action="store_true")
    args = p.parse_args()

    symbol = args.symbol.upper().strip()
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    if end < start:
        raise SystemExit("end must be >= start")

    out_dir = pathlib.Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    frame, meta = build(symbol, start, end, args.freq, not args.no_checksum)
    parquet = out_dir / f"{symbol}-{args.freq}-{start.isoformat()}_{end.isoformat()}.parquet"
    meta_path = parquet.with_suffix(".json")
    frame.to_parquet(parquet, index=False)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "kind": "binance_vision_historical_feature_layer_complete",
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "parquet": str(parquet),
        "metadata": str(meta_path),
        "rows": len(frame),
        "failures": len(meta["failures"]),
    }, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
