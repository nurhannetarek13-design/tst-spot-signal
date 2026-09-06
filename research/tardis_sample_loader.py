#!/usr/bin/env python3
"""Tardis.dev free-sample loader for Binance USD-M microstructure research.

Free historical CSV datasets are available for the first day of each month
without an API key. Timestamps are microseconds since epoch.

Important: Binance Futures liquidations are distributed via the special
PERPETUALS dataset; filter the returned rows by `symbol` after loading.
"""
from __future__ import annotations

import csv
import gzip
import io
import urllib.request
from typing import Iterable

BASE = "https://datasets.tardis.dev/v1/binance-futures"
UA = "tst-tardis-sample-loader/1.0"


def normalize_us_to_ms(value: int | str) -> int:
    return int(value) // 1000


def _download_csv_gz(url: str) -> list[dict[str, str]]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=90) as r:
        payload = r.read()
    text = gzip.decompress(payload).decode("utf-8-sig", "replace")
    return list(csv.DictReader(io.StringIO(text)))


def sample_url(data_type: str, year: int, month: int, symbol: str) -> str:
    symbol = symbol.upper()
    if data_type == "liquidations":
        symbol = "PERPETUALS"
    return f"{BASE}/{data_type}/{year:04d}/{month:02d}/01/{symbol}.csv.gz"


def load_monthly_sample(data_type: str, year: int, month: int, symbol: str) -> list[dict]:
    if data_type not in {"liquidations", "trades", "book_ticker", "derivative_ticker", "incremental_book_L2", "book_snapshot_25", "book_snapshot_5"}:
        raise ValueError(f"unsupported Tardis data type: {data_type}")
    wanted = symbol.upper()
    rows = _download_csv_gz(sample_url(data_type, year, month, wanted))
    out = []
    for row in rows:
        if data_type == "liquidations" and row.get("symbol", "").upper() != wanted:
            continue
        parsed = dict(row)
        for key in ("timestamp", "local_timestamp", "funding_timestamp"):
            if parsed.get(key):
                parsed[key + "_ms"] = normalize_us_to_ms(parsed[key])
        out.append(parsed)
    return out


def load_first_day_samples(data_type: str, symbol: str, months: Iterable[tuple[int, int]]) -> list[dict]:
    rows = []
    for year, month in months:
        rows.extend(load_monthly_sample(data_type, year, month, symbol))
    return rows
