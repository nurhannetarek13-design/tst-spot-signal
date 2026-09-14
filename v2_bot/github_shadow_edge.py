from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .binance_public import BinancePublicClient
from .external_btc_edge import (
    STRATEGY_ID,
    SYMBOL,
    _fetch_funding_rows,
    dump_state,
    load_state,
    process_cycle,
)


def _market_snapshot() -> tuple[list[dict[str, Any]], float, float]:
    client = BinancePublicClient(timeout=15.0)
    try:
        candles = client.klines(SYMBOL, "1h", 1000, closed_only=True)
        if len(candles) < 601:
            raise RuntimeError("insufficient_closed_hourly_candles")
        book = client.book_tickers().get(SYMBOL, {})
        ask = float(book.get("ask", 0.0) or 0.0)
        bid = float(book.get("bid", 0.0) or 0.0)
        if ask <= 0 or bid <= 0:
            raise RuntimeError("invalid_book_ticker")
        return candles, ask, bid
    finally:
        client.close()


def run_once(state_path: Path, quote_size_usdt: float) -> dict[str, Any]:
    state = load_state(state_path.read_text() if state_path.exists() else None)
    candles, ask, bid = _market_snapshot()
    funding = _fetch_funding_rows()
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
        "promotion_eligible": False,
        "live_eligible": False,
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
