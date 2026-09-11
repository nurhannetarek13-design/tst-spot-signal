#!/usr/bin/env python3
"""Download and convert canonical Tardis Spot data for hftbacktest.

Produces BOTH non-fused L2 and L2+native-bookTicker files so execution results can
be checked for feed sensitivity. No candle data and no synthetic fills are used.
"""
import argparse
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

from hftbacktest.data import validate_event_order
from hftbacktest.data.utils import tardis
import numpy as np


def download(url: str, out: Path, key: str | None) -> None:
    if out.exists() and out.stat().st_size > 0:
        return
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    req = Request(url, headers=headers)
    with urlopen(req, timeout=120) as r, out.open("wb") as w:
        while True:
            b = r.read(1024 * 1024)
            if not b:
                break
            w.write(b)


def dataset_url(exchange, dtype, date, symbol):
    y, m, d = date.split("-")
    return f"https://datasets.tardis.dev/v1/{exchange}/{dtype}/{y}/{m}/{d}/{symbol}.csv.gz"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--exchange", default="binance")
    p.add_argument("--symbol", required=True)
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--tick-size", required=True, type=float)
    p.add_argument("--lot-size", required=True, type=float)
    p.add_argument("--out-dir", default=".cache/tardis-hft")
    p.add_argument("--buffer-size", type=int, default=150_000_000)
    a = p.parse_args()

    root = Path(a.out_dir) / a.symbol / a.date
    root.mkdir(parents=True, exist_ok=True)
    key = os.getenv("TARDIS_KEY") or os.getenv("TARDIS_API_KEY")
    ymd = a.date.replace("-", "")
    paths = {}
    for dtype in ("trades", "incremental_book_L2", "book_ticker"):
        f = root / f"{a.exchange}_{dtype}_{a.symbol}_{ymd}.csv.gz"
        download(dataset_url(a.exchange, dtype, a.date, a.symbol), f, key)
        paths[dtype] = f

    l2 = root / f"{a.symbol}_{ymd}_l2.npz"
    fused = root / f"{a.symbol}_{ymd}_l2_tob.npz"
    tardis.convert(
        [str(paths["trades"]), str(paths["incremental_book_L2"])],
        output_filename=str(l2),
        buffer_size=a.buffer_size,
        snapshot_mode="process",
    )
    tardis.convert_fuse(
        str(paths["trades"]),
        str(paths["incremental_book_L2"]),
        str(paths["book_ticker"]),
        tick_size=a.tick_size,
        lot_size=a.lot_size,
        output_filename=str(fused),
    )

    report = {"symbol": a.symbol, "date": a.date, "exchange": a.exchange, "files": {}}
    for name, f in (("L2_ONLY", l2), ("L2_PLUS_BOOKTICKER", fused)):
        arr = np.load(f)["data"]
        validate_event_order(arr)
        report["files"][name] = {"path": str(f), "events": int(len(arr)), "bytes": int(f.stat().st_size)}
    report["canonical"] = True
    report["authorization"] = "RESEARCH_ONLY"
    report["liveTrading"] = False
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
