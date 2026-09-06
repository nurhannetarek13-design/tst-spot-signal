#!/usr/bin/env python3
"""Research-only Coinalyze historical liquidation loader.

Purpose
-------
Fetch daily aggregated long/short liquidation history for Binance USD-M
perpetual markets (BTCUSDT, ETHUSDT, SOLUSDT by default). This is a historical
regime feature source, NOT tick-level liquidation data and NOT a trading signal.

Auth
----
Set COINALYZE_API_KEY in the environment. The key is sent in the `api_key`
HTTP header and is never printed.

Output
------
Writes one CSV and one JSON metadata file under data/coinalyze-liquidations/.
The CSV columns are:
  timestamp_utc,symbol,coinalyze_symbol,long_liquidations,short_liquidations,
  total_liquidations,imbalance,interval,source

`imbalance` = (short - long) / (short + long), bounded to [-1, 1] when total>0.
Positive values mean short liquidations dominate; negative values mean long
liquidations dominate.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.coinalyze.net/v1"
API_KEY_ENV = "COINALYZE_API_KEY"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
DEFAULT_DAYS = 365
DEFAULT_INTERVAL = "daily"
OUTPUT_DIR = pathlib.Path(os.getenv("COINALYZE_DATA_DIR", "data/coinalyze-liquidations"))
AUTHORIZATION = "RESEARCH_ONLY"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def unix_seconds(x: dt.datetime) -> int:
    return int(x.timestamp())


def api_get(path: str, api_key: str, params: dict[str, str | int] | None = None):
    query = urllib.parse.urlencode(params or {})
    url = f"{BASE_URL}/{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(
        url,
        headers={
            "api_key": api_key,
            "Accept": "application/json",
            "User-Agent": "tst-research-coinalyze-loader/1.0",
        },
    )
    while True:
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry = int(exc.headers.get("Retry-After", "2") or "2")
                time.sleep(max(1, min(retry, 60)))
                continue
            body = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Coinalyze HTTP {exc.code} for {path}: {body}") from exc


def discover_binance_code(api_key: str) -> str:
    exchanges = api_get("exchanges", api_key)
    matches = [x for x in exchanges if "binance" in str(x.get("name", "")).lower()]
    if not matches:
        raise RuntimeError("Binance was not found in Coinalyze /exchanges response")
    # Prefer the plain Binance venue if multiple branded variants exist.
    exact = [x for x in matches if str(x.get("name", "")).strip().lower() == "binance"]
    chosen = exact[0] if exact else matches[0]
    code = str(chosen.get("code") or "").strip()
    if not code:
        raise RuntimeError("Coinalyze Binance exchange entry has no code")
    return code


def discover_symbols(api_key: str, requested: tuple[str, ...], binance_code: str) -> dict[str, str]:
    markets = api_get("future-markets", api_key)
    wanted = {s.upper(): None for s in requested}
    for m in markets:
        if str(m.get("exchange") or "") != binance_code:
            continue
        if not bool(m.get("is_perpetual")):
            continue
        if str(m.get("margined") or "").upper() != "STABLE":
            continue
        symbol_on_exchange = str(m.get("symbol_on_exchange") or "").upper()
        if symbol_on_exchange in wanted and wanted[symbol_on_exchange] is None:
            wanted[symbol_on_exchange] = str(m.get("symbol") or "")
    missing = [k for k, v in wanted.items() if not v]
    if missing:
        raise RuntimeError(f"Missing Binance stable perpetual markets in Coinalyze: {missing}")
    return {k: str(v) for k, v in wanted.items()}


def fetch_history(api_key: str, symbol_map: dict[str, str], start: dt.datetime, end: dt.datetime, interval: str):
    symbols_csv = ",".join(symbol_map.values())
    payload = api_get(
        "liquidation-history",
        api_key,
        {
            "symbols": symbols_csv,
            "interval": interval,
            "from": unix_seconds(start),
            "to": unix_seconds(end),
            "convert_to_usd": "true",
        },
    )
    reverse = {v: k for k, v in symbol_map.items()}
    rows: list[dict] = []
    for block in payload:
        cz_symbol = str(block.get("symbol") or "")
        local_symbol = reverse.get(cz_symbol, cz_symbol)
        for point in block.get("history") or []:
            ts = int(point.get("t") or 0)
            long_liq = float(point.get("l") or 0.0)
            short_liq = float(point.get("s") or 0.0)
            total = long_liq + short_liq
            imbalance = ((short_liq - long_liq) / total) if total > 0 else 0.0
            rows.append(
                {
                    "timestamp_utc": dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat(),
                    "symbol": local_symbol,
                    "coinalyze_symbol": cz_symbol,
                    "long_liquidations": long_liq,
                    "short_liquidations": short_liq,
                    "total_liquidations": total,
                    "imbalance": imbalance,
                    "interval": interval,
                    "source": "Coinalyze",
                }
            )
    rows.sort(key=lambda r: (r["timestamp_utc"], r["symbol"]))
    return rows


def write_outputs(rows: list[dict], metadata: dict, output_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"binance-usdm-liquidations-{metadata['interval']}-{metadata['fromDate']}-{metadata['toDate']}"
    csv_path = output_dir / f"{stem}.csv"
    json_path = output_dir / f"{stem}.metadata.json"
    fieldnames = [
        "timestamp_utc", "symbol", "coinalyze_symbol", "long_liquidations",
        "short_liquidations", "total_liquidations", "imbalance", "interval", "source",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    return csv_path, json_path


def self_test() -> None:
    sample = {"t": 1704067200, "l": 75.0, "s": 25.0}
    total = sample["l"] + sample["s"]
    imbalance = (sample["s"] - sample["l"]) / total
    assert total == 100.0
    assert abs(imbalance + 0.5) < 1e-12
    print(json.dumps({"authorization": AUTHORIZATION, "liveTrading": False, "selfTest": "PASS"}))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--interval", default=DEFAULT_INTERVAL, choices=["daily", "1hour", "2hour", "4hour", "6hour", "12hour"])
    p.add_argument("--output-dir", default=str(OUTPUT_DIR))
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        self_test()
        return

    api_key = os.getenv(API_KEY_ENV, "").strip()
    if not api_key:
        raise SystemExit(f"Missing required environment variable: {API_KEY_ENV}")

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    end = utc_now().replace(microsecond=0)
    start = end - dt.timedelta(days=max(1, args.days))

    binance_code = discover_binance_code(api_key)
    symbol_map = discover_symbols(api_key, symbols, binance_code)
    rows = fetch_history(api_key, symbol_map, start, end, args.interval)

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["symbol"]] = counts.get(row["symbol"], 0) + 1
    metadata = {
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "source": "Coinalyze",
        "datasetType": "aggregated_liquidation_history",
        "tickLevel": False,
        "exchange": "Binance",
        "exchangeCode": binance_code,
        "symbolMap": symbol_map,
        "interval": args.interval,
        "fromDate": start.date().isoformat(),
        "toDate": end.date().isoformat(),
        "rows": len(rows),
        "rowsBySymbol": counts,
        "limitations": [
            "Aggregated liquidation values per bucket, not individual liquidation events.",
            "Binance liquidation feed itself is throttled; this dataset must not be described as complete tick-level liquidation history.",
        ],
    }
    csv_path, meta_path = write_outputs(rows, metadata, pathlib.Path(args.output_dir))
    print(json.dumps({"status": "OK", "csv": str(csv_path), "metadata": str(meta_path), **metadata}, indent=2))


if __name__ == "__main__":
    main()
