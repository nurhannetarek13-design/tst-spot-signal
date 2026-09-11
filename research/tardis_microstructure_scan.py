#!/usr/bin/env python3
"""Fetch ordered Tardis Binance Futures raw L2 and produce canonical diagnostics.

Research only. This script never places orders and deliberately keeps live
trading disabled.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import urllib.parse

from research.tardis_l2_replay import parse_ordered_raw
from research.tardis_microstructure_features import (
    extract_microstructure_rows,
    frozen_gate_g_diagnostic,
    sample_last_per_second,
    summarize_features,
)

BASE = "https://api.tardis.dev/v1/data-feeds/binance-futures"
AUTHORIZATION = "RESEARCH_ONLY"


def fetch_combined(symbol: str, date: str, offset: int) -> str:
    filters = [
        {"channel": "depthSnapshot", "symbols": [symbol.lower()]},
        {"channel": "depth", "symbols": [symbol.lower()]},
        {"channel": "bookTicker", "symbols": [symbol.lower()]},
    ]
    encoded = urllib.parse.quote(json.dumps(filters, separators=(",", ":")), safe='[]{}\":,')
    url = (
        f"{BASE}?from={urllib.parse.quote(date, safe=':-TZ')}"
        f"&filters={encoded}&offset={offset}"
    )
    p = subprocess.run(
        [
            "curl", "--compressed", "-sS", "-g",
            "--connect-timeout", "15",
            "--max-time", "90",
            "--retry", "3",
            "--retry-all-errors",
            "--retry-delay", "2",
            url,
        ],
        capture_output=True,
        check=True,
        timeout=300,
    )
    return p.stdout.decode("utf-8", "replace")


def scan(symbol: str, date: str, offset: int) -> dict:
    body = fetch_combined(symbol, date, offset)
    rows = parse_ordered_raw(body)
    extracted = extract_microstructure_rows(rows, symbol=symbol, date=date, offset=offset)
    features = extracted.get("features", [])
    samples = sample_last_per_second(features)
    diagnostic = frozen_gate_g_diagnostic(samples)
    validation = extracted.get("validation", {})
    ready = bool(
        extracted.get("status") == "PASS"
        and validation.get("canonicalReplayReady")
        and features
        and samples
    )
    return {
        "engine": "TARDIS_CANONICAL_MICROSTRUCTURE_V1",
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "symbol": symbol,
        "date": date,
        "offset": offset,
        "rawBytes": len(body.encode("utf-8")),
        "rawRows": len(rows),
        "status": "PASS" if ready else "FAIL",
        "canonicalReplayReady": bool(validation.get("canonicalReplayReady")),
        "validation": validation,
        "summary": summarize_features(features, samples),
        "gateG": diagnostic,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", required=True, choices=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    p.add_argument("--date", default="2021-09-01")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--output")
    p.add_argument("--assert-ready", action="store_true")
    a = p.parse_args()
    result = scan(a.symbol, a.date, a.offset)
    text = json.dumps(result, indent=2, sort_keys=True)
    if a.output:
        path = pathlib.Path(a.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(text)
    if a.assert_ready and result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
