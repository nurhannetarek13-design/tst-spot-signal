#!/usr/bin/env python3
"""Research-only probe for Binance Vision USD-M futures metrics archives.

Downloads exactly one daily metrics ZIP, verifies its checksum, prints the
observed schema and a few rows, and writes a small JSON report. No trading.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import pathlib
import urllib.request
import zipfile

import pandas as pd

BASE = "https://data.binance.vision/data/futures/um/daily/metrics"
UA = "tst-binance-vision-metrics-probe/1.0"


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--date", default="2025-01-02")
    p.add_argument("--output", default="artifacts/binance-vision-metrics-probe.json")
    args = p.parse_args()

    symbol = args.symbol.upper().strip()
    filename = f"{symbol}-metrics-{args.date}.zip"
    url = f"{BASE}/{symbol}/{filename}"
    raw = get(url)
    checksum_text = get(url + ".CHECKSUM").decode("utf-8", errors="replace").strip()
    expected = checksum_text.split()[0].lower()
    actual = hashlib.sha256(raw).hexdigest()
    if expected != actual:
        raise RuntimeError(f"checksum mismatch expected={expected} actual={actual}")

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        csvs = [n for n in zf.namelist() if n.lower().endswith('.csv')]
        if len(csvs) != 1:
            raise RuntimeError(f"expected one csv, got {csvs}")
        with zf.open(csvs[0]) as f:
            df = pd.read_csv(f)

    report = {
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "symbol": symbol,
        "date": args.date,
        "url": url,
        "checksumVerified": True,
        "rows": int(len(df)),
        "columns": [str(c) for c in df.columns],
        "dtypes": {str(c): str(t) for c, t in df.dtypes.items()},
        "sample": json.loads(df.head(3).to_json(orient="records")),
    }
    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, separators=(",", ":")))


if __name__ == "__main__":
    main()
