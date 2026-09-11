#!/usr/bin/env python3
"""Fetch ordered Tardis raw data and validate canonical Binance USD-M L2 replay.

Research-only. No trading actions.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import urllib.parse

from tardis_l2_replay import parse_ordered_raw, replay_ordered_rows

BASE = "https://api.tardis.dev/v1/data-feeds/binance-futures"
AUTHORIZATION = "RESEARCH_ONLY"


def fetch_combined(symbol: str, date: str, offset: int) -> str:
    filters = [
        {"channel": "depthSnapshot", "symbols": [symbol.lower()]},
        {"channel": "depth", "symbols": [symbol.lower()]},
        {"channel": "bookTicker", "symbols": [symbol.lower()]},
    ]
    encoded = urllib.parse.quote(
        json.dumps(filters, separators=(",", ":")), safe='[]{}\":,'
    )
    url = (
        f"{BASE}?from={urllib.parse.quote(date, safe=':-TZ')}"
        f"&filters={encoded}&offset={offset}"
    )
    p = subprocess.run(
        ["curl", "--compressed", "-sS", "-g", url],
        capture_output=True,
        check=True,
    )
    return p.stdout.decode("utf-8", "replace")


def validate(symbol: str, date: str, offset: int) -> dict:
    rows = parse_ordered_raw(fetch_combined(symbol, date, offset))
    return replay_ordered_rows(rows, symbol=symbol, date=date, offset=offset)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--symbol",
        default="BTCUSDT",
        choices=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    )
    p.add_argument("--date", default="2021-09-01")
    p.add_argument("--offset", type=int, default=0)
    a = p.parse_args()
    result = validate(a.symbol, a.date, a.offset)
    print(
        json.dumps(
            {"authorization": AUTHORIZATION, "liveTrading": False, **result},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
