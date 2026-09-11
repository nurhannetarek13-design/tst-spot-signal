#!/usr/bin/env python3
"""Replay a contiguous multi-minute Tardis Binance Futures L2 window.

Research only. The first minute supplies the depth snapshot; subsequent minute
slices extend the same ordered depth-update chain. No trading actions exist in
this module.
"""
from __future__ import annotations

import argparse
import json
import pathlib

from research.tardis_l2_replay import parse_ordered_raw
from research.tardis_microstructure_features import (
    extract_microstructure_rows,
    frozen_gate_g_diagnostic,
    sample_last_per_second,
    summarize_features,
)
from research.tardis_microstructure_scan import fetch_combined

AUTHORIZATION = "RESEARCH_ONLY"


def fetch_window(symbol: str, date: str, start_offset: int, minutes: int) -> str:
    if minutes < 1:
        raise ValueError("minutes must be >= 1")
    parts: list[str] = []
    for offset in range(start_offset, start_offset + minutes):
        body = fetch_combined(symbol, date, offset)
        if body:
            parts.append(body.rstrip("\n"))
    return "\n".join(parts) + ("\n" if parts else "")


def scan_range(symbol: str, date: str, start_offset: int, minutes: int) -> dict:
    body = fetch_window(symbol, date, start_offset, minutes)
    rows = parse_ordered_raw(body)
    extracted = extract_microstructure_rows(
        rows,
        symbol=symbol,
        date=date,
        offset=start_offset,
    )
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
        "engine": "TARDIS_CANONICAL_MICROSTRUCTURE_RANGE_V1",
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "symbol": symbol,
        "date": date,
        "startOffset": start_offset,
        "minutes": minutes,
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
    p.add_argument("--start-offset", type=int, default=0)
    p.add_argument("--minutes", type=int, default=30)
    p.add_argument("--output")
    p.add_argument("--assert-ready", action="store_true")
    a = p.parse_args()
    result = scan_range(a.symbol, a.date, a.start_offset, a.minutes)
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
