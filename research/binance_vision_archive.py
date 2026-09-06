#!/usr/bin/env python3
"""Verified Binance Vision historical archive loader.

Research-only utilities for public Spot/USD-M/COIN-M data. Downloads ZIP archives
from https://data.binance.vision, verifies sibling SHA256 checksums, normalizes Spot
microsecond timestamps to milliseconds, and exposes stable market/derivatives rows
for deterministic backtests.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import os
import pathlib
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://data.binance.vision/data"
CACHE = pathlib.Path(os.environ.get("BINANCE_VISION_CACHE", ".cache/binance-vision"))
UA = "tst-binance-vision-archive/1.1"


def normalize_epoch_ms(value: int | str) -> int:
    x = int(value)
    if x >= 100_000_000_000_000:
        return x // 1000
    return x


def _request_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read()


def _cache_path(url: str) -> pathlib.Path:
    rel = url.split("https://data.binance.vision/", 1)[-1]
    return CACHE / rel


def _download_verified_zip(url: str) -> bytes:
    target = _cache_path(url)
    checksum_target = pathlib.Path(str(target) + ".CHECKSUM")
    if target.exists() and checksum_target.exists():
        payload = target.read_bytes()
        expected = checksum_target.read_text().strip().split()[0].lower()
        if hashlib.sha256(payload).hexdigest().lower() == expected:
            return payload

    payload = _request_bytes(url)
    checksum = _request_bytes(url + ".CHECKSUM").decode("utf-8", "replace").strip()
    expected = checksum.split()[0].lower()
    actual = hashlib.sha256(payload).hexdigest().lower()
    if not expected or actual != expected:
        raise RuntimeError(f"checksum mismatch for {url}: expected={expected} actual={actual}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    checksum_target.write_text(checksum + "\n")
    return payload


def _csv_rows_from_zip(url: str) -> list[list[str]]:
    payload = _download_verified_zip(url)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if not names:
            return []
        text = zf.read(names[0]).decode("utf-8-sig", "replace")
    return [row for row in csv.reader(io.StringIO(text)) if row]


def _month_iter(start: dt.date, end_exclusive: dt.date):
    cur = dt.date(start.year, start.month, 1)
    while cur < end_exclusive:
        yield cur.year, cur.month
        cur = dt.date(cur.year + (cur.month == 12), 1 if cur.month == 12 else cur.month + 1, 1)


def _day_iter(start: dt.date, end_exclusive: dt.date):
    cur = start
    while cur < end_exclusive:
        yield cur
        cur += dt.timedelta(days=1)


def _is_header(row: list[str]) -> bool:
    if not row:
        return True
    try:
        int(row[0])
        return False
    except (TypeError, ValueError):
        return True


def _market_root(market: str) -> str:
    if market == "spot":
        return "spot"
    if market in {"um", "cm"}:
        return f"futures/{market}"
    raise ValueError("market must be 'spot', 'um', or 'cm'")


def _kline_url(market: str, frequency: str, symbol: str, interval: str, stamp: str) -> str:
    return f"{BASE}/{_market_root(market)}/{frequency}/klines/{symbol}/{interval}/{symbol}-{interval}-{stamp}.zip"


def load_klines(market: str, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list[str]]:
    symbol = symbol.upper()
    start_date = dt.datetime.fromtimestamp(start_ms / 1000, tz=dt.timezone.utc).date()
    end_date = dt.datetime.fromtimestamp(max(start_ms, end_ms - 1) / 1000, tz=dt.timezone.utc).date() + dt.timedelta(days=1)
    today = dt.datetime.now(dt.timezone.utc).date()
    current_month = dt.date(today.year, today.month, 1)

    urls: list[str] = []
    for y, m in _month_iter(start_date, end_date):
        month_start = dt.date(y, m, 1)
        next_month = dt.date(y + (m == 12), 1 if m == 12 else m + 1, 1)
        if next_month <= current_month:
            urls.append(_kline_url(market, "monthly", symbol, interval, f"{y:04d}-{m:02d}"))
        else:
            lo = max(start_date, month_start)
            hi = min(end_date, today)
            for day in _day_iter(lo, hi):
                urls.append(_kline_url(market, "daily", symbol, interval, day.isoformat()))

    rows: list[list[str]] = []
    failures: list[str] = []
    for url in urls:
        try:
            part = _csv_rows_from_zip(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                failures.append(url)
                continue
            raise
        for row in part:
            if _is_header(row) or len(row) < 12:
                continue
            row = list(row)
            row[0] = str(normalize_epoch_ms(row[0]))
            row[6] = str(normalize_epoch_ms(row[6]))
            ts = int(row[0])
            if start_ms <= ts < end_ms:
                rows.append(row)

    dedup = {int(r[0]): r for r in rows}
    out = [dedup[k] for k in sorted(dedup)]
    if not out:
        suffix = f"; missing archives={len(failures)}" if failures else ""
        raise RuntimeError(f"no {market} klines for {symbol} {interval}{suffix}")
    return out


def _metrics_url(market: str, symbol: str, day: dt.date) -> str:
    return f"{BASE}/futures/{market}/daily/metrics/{symbol}/{symbol}-metrics-{day.isoformat()}.zip"


def _load_metrics_day(market: str, symbol: str, day: dt.date) -> tuple[dt.date, list[dict]]:
    url = _metrics_url(market, symbol, day)
    try:
        rows = _csv_rows_from_zip(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return day, []
        raise
    out: list[dict] = []
    for row in rows:
        if not row or row[0].strip().lower() == "create_time" or len(row) < 8:
            continue
        try:
            when = dt.datetime.strptime(row[0].strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        out.append({
            "timestamp": int(when.timestamp() * 1000),
            "symbol": row[1].strip(),
            "sumOpenInterest": row[2].strip(),
            "sumOpenInterestValue": row[3].strip(),
            "countTopTraderLongShortRatio": row[4].strip(),
            "sumTopTraderLongShortRatio": row[5].strip(),
            "countLongShortRatio": row[6].strip(),
            "buySellRatio": row[7].strip(),
        })
    return day, out


def load_futures_metrics(market: str, symbol: str, start_ms: int, end_ms: int, workers: int = 8) -> tuple[list[dict], dict]:
    if market not in {"um", "cm"}:
        raise ValueError("metrics market must be 'um' or 'cm'")
    symbol = symbol.upper()
    start_date = dt.datetime.fromtimestamp(start_ms / 1000, tz=dt.timezone.utc).date()
    end_date = dt.datetime.fromtimestamp(max(start_ms, end_ms - 1) / 1000, tz=dt.timezone.utc).date() + dt.timedelta(days=1)
    today = dt.datetime.now(dt.timezone.utc).date()
    days = list(_day_iter(start_date, min(end_date, today)))
    loaded: dict[dt.date, list[dict]] = {}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_load_metrics_day, market, symbol, day): day for day in days}
        for fut in as_completed(futures):
            day, rows = fut.result()
            loaded[day] = rows

    rows = []
    missing_days = []
    for day in days:
        part = loaded.get(day, [])
        if not part:
            missing_days.append(day.isoformat())
        rows.extend(part)

    raw_count = len(rows)
    dedup = {int(r["timestamp"]): r for r in rows if start_ms <= int(r["timestamp"]) < end_ms}
    out = [dedup[k] for k in sorted(dedup)]
    expected_5m = max(0, (end_ms - start_ms) // (5 * 60 * 1000))
    quality = {
        "market": market,
        "rawRows": raw_count,
        "uniqueRows": len(out),
        "duplicateRows": max(0, raw_count - len(dedup)),
        "missingArchiveDays": missing_days,
        "expected5mRowsApprox": expected_5m,
        "coveragePctApprox": round(100 * len(out) / expected_5m, 3) if expected_5m else None,
    }
    return out, quality


def load_um_metrics(symbol: str, start_ms: int, end_ms: int, workers: int = 8) -> tuple[list[dict], dict]:
    return load_futures_metrics("um", symbol, start_ms, end_ms, workers)


def _funding_url(market: str, symbol: str, year: int, month: int) -> str:
    return f"{BASE}/futures/{market}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{year:04d}-{month:02d}.zip"


def load_funding_rates(market: str, symbol: str, start_ms: int, end_ms: int) -> tuple[list[dict], dict]:
    if market not in {"um", "cm"}:
        raise ValueError("funding market must be 'um' or 'cm'")
    symbol = symbol.upper()
    start_date = dt.datetime.fromtimestamp(start_ms / 1000, tz=dt.timezone.utc).date()
    end_date = dt.datetime.fromtimestamp(max(start_ms, end_ms - 1) / 1000, tz=dt.timezone.utc).date() + dt.timedelta(days=1)
    rows: list[dict] = []
    missing_months: list[str] = []
    for y, m in _month_iter(start_date, end_date):
        url = _funding_url(market, symbol, y, m)
        try:
            part = _csv_rows_from_zip(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                missing_months.append(f"{y:04d}-{m:02d}")
                continue
            raise
        for row in part:
            if not row:
                continue
            lower = [x.strip().lower() for x in row]
            if "calc_time" in lower or "funding_rate" in lower or "fundingrate" in lower:
                continue
            ts = None
            rate = None
            for value in row:
                v = value.strip()
                if ts is None:
                    try:
                        candidate = normalize_epoch_ms(v)
                        if candidate > 1_000_000_000_000:
                            ts = candidate
                            continue
                    except Exception:
                        pass
                if rate is None:
                    try:
                        f = float(v)
                        if abs(f) < 1:
                            rate = f
                    except Exception:
                        pass
            if ts is not None and rate is not None and start_ms <= ts < end_ms:
                rows.append({"timestamp": ts, "symbol": symbol, "fundingRate": rate})
    dedup = {int(r["timestamp"]): r for r in rows}
    out = [dedup[k] for k in sorted(dedup)]
    return out, {"market": market, "rows": len(out), "missingArchiveMonths": missing_months}


def _liquidation_url_cm(symbol: str, day: dt.date) -> str:
    return f"{BASE}/futures/cm/daily/liquidationSnapshot/{symbol}/{symbol}-liquidationSnapshot-{day.isoformat()}.zip"


def load_cm_liquidation_snapshots(symbol: str, start_ms: int, end_ms: int, workers: int = 8) -> tuple[list[list[str]], dict]:
    symbol = symbol.upper()
    start_date = dt.datetime.fromtimestamp(start_ms / 1000, tz=dt.timezone.utc).date()
    end_date = dt.datetime.fromtimestamp(max(start_ms, end_ms - 1) / 1000, tz=dt.timezone.utc).date() + dt.timedelta(days=1)
    today = dt.datetime.now(dt.timezone.utc).date()
    days = list(_day_iter(start_date, min(end_date, today)))

    def one(day: dt.date):
        try:
            return day, _csv_rows_from_zip(_liquidation_url_cm(symbol, day))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return day, []
            raise

    loaded = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, day): day for day in days}
        for fut in as_completed(futures):
            day, part = fut.result()
            loaded[day] = part

    rows = []
    missing_days = []
    for day in days:
        part = loaded.get(day, [])
        if not part:
            missing_days.append(day.isoformat())
        rows.extend(part)
    return rows, {"market": "cm", "rows": len(rows), "missingArchiveDays": missing_days}
