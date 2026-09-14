from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx

from .external_btc_edge import (
    STRATEGY_ID,
    SYMBOL,
    _fetch_funding_rows,
    default_state,
    dump_state,
    load_state,
    process_cycle,
)

SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"
SPOT_BOOK_URL = "https://api.binance.com/api/v3/ticker/bookTicker"


def _closed_hourly_candles(timeout: float = 15.0) -> list[dict[str, Any]]:
    response = httpx.get(
        SPOT_KLINES_URL,
        params={"symbol": SYMBOL, "interval": "1h", "limit": 1000},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("unexpected_spot_klines_response")
    now_ms = int(time.time() * 1000)
    candles: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 7:
            continue
        close_time = int(row[6])
        if close_time >= now_ms:
            continue
        candles.append(
            {
                "open_time": float(row[0]),
                "close_time": float(close_time),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
            }
        )
    if len(candles) < 601:
        raise RuntimeError("insufficient_closed_hourly_candles")
    return candles


def _book(timeout: float = 10.0) -> tuple[float, float]:
    response = httpx.get(
        SPOT_BOOK_URL,
        params={"symbol": SYMBOL},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected_book_ticker_response")
    ask = float(payload.get("askPrice", 0.0) or 0.0)
    bid = float(payload.get("bidPrice", 0.0) or 0.0)
    if ask <= 0 or bid <= 0:
        raise RuntimeError("invalid_book_ticker")
    return ask, bid


def run_once(state_path: Path, quote_size_usdt: float) -> dict[str, Any]:
    state = load_state(state_path.read_text() if state_path.exists() else None)
    candles = _closed_hourly_candles()
    funding = _fetch_funding_rows()
    ask, bid = _book()
    state, result = process_cycle(
        state=state,
        candles=candles,
        funding_rows=funding,
        quote_size_usdt=quote_size_usdt,
        entry_ask=ask,
        exit_bid=bid,
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(dump_state(state) + "\n")
    return {
        "runner": "github_actions_shadow",
        "strategy_id": STRATEGY_ID,
        "symbol": SYMBOL,
        "research_only": True,
        "live_trading": False,
        "result": result,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=".runtime/btc_ema600_funding_state.json")
    parser.add_argument("--quote-size", type=float, default=10.0)
    args = parser.parse_args()
    if args.quote_size <= 0 or args.quote_size > 10.0:
        raise SystemExit("quote size must be >0 and <=10 USDT")
    output = run_once(Path(args.state), args.quote_size)
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
