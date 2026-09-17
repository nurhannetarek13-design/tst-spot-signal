"""Read-only candidate discovery with an auditable, gap-free OHLCV snapshot.

Never uses private Binance endpoints, trades, changes deployment flags or grants
strategy approval for live trading. A missing candle fails the entire report.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

import httpx

from .candidate_discovery import run_discovery
from .config import Settings
from .historical_replay import DEFAULT_SYMBOLS, fetch_spot_15m
from .research_data_gate import validate_closed_history


def main() -> None:
    parser = argparse.ArgumentParser(description="Gap-safe diagnostic Binance Spot discovery")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--output", default="v2_safe_candidate_discovery.json")
    args = parser.parse_args()
    if not 90 <= args.days <= 365:
        parser.error("days must be 90..365")
    if not 5.0 <= args.slippage_bps <= 100.0:
        parser.error("slippage-bps must be 5..100; do not zero-out execution costs")
    symbols = tuple(dict.fromkeys(x.strip().upper() for x in args.symbols.split(",") if x.strip()))
    if "BTCUSDT" not in symbols:
        symbols = ("BTCUSDT", *symbols)

    snapshot = datetime.now(timezone.utc)
    end_ms = int(snapshot.timestamp() * 1000)
    start_ms = int((snapshot - timedelta(days=args.days)).timestamp() * 1000)
    data = {}
    with httpx.Client(timeout=30.0, headers={"User-Agent":"tst-v2-gap-safe-research/1.0"}) as client:
        for symbol in symbols:
            downloaded = fetch_spot_15m(symbol, start_ms=start_ms, end_ms=end_ms, client=client)
            candles = validate_closed_history(
                symbol, downloaded, start_ms=start_ms, end_ms=end_ms, as_of_ms=end_ms,
            )
            if len(candles) < 5000:
                raise RuntimeError(f"insufficient_valid_history:{symbol}:{len(candles)}")
            data[symbol] = candles

    report = run_discovery(data, settings=Settings(mode="shadow"), slippage_bps=args.slippage_bps)
    report["period_days"] = args.days
    report["symbols"] = list(symbols)
    report["data_integrity"] = {
        "verdict": "COMPLETE_CONTIGUOUS_CLOSED_15M_OR_FAIL",
        "as_of_utc": snapshot.isoformat(),
        "candles_by_symbol": {symbol: len(rows) for symbol, rows in data.items()},
    }
    report["authorization"] = "RESEARCH_ONLY_NOT_TRADING_APPROVAL"
    report["live_trading"] = False
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print(json.dumps({
        "authorization": report["authorization"],
        "data_integrity": report["data_integrity"],
        "eligible_families": report["eligible_families"],
        "families": {name: {"accepted": result["accepted"],
                            "discovery": result["discovery"]["stats"],
                            "oos": result["oos"]["stats"]}
                     for name, result in report["families"].items()},
        "live_trading": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
