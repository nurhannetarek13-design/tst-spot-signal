"""Run three independent zero-credential, forward-only Spot strategy simulations.

Usage: python -m v2_bot.pionex_run --once
No private Binance client, API credentials or real order route is imported.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from .binance_public import BinancePublicClient
from .pionex_ledger_guard import validate_ledger
from .pionex_style import MODES, Rules, new_state, step

BAR_MS = 900_000
HOUR_MS = 3_600_000
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}


def _continuous(candles: list[dict[str, float]], width: int, length: int) -> None:
    if len(candles) < length:
        raise RuntimeError("insufficient_historical_bars")
    times = [int(row["open_time"]) for row in candles[-length:]]
    if any(b - a != width for a, b in zip(times, times[1:])):
        raise RuntimeError("missing_or_duplicate_market_bar_fail_closed")
    if any(row["high"] < max(row["open"], row["close"])
           or row["low"] > min(row["open"], row["close"])
           or row["low"] <= 0 for row in candles[-length:]):
        raise RuntimeError("malformed_market_bar_fail_closed")


def run_once(*, symbol: str = "BTCUSDT", budget: float = 50.0,
             path: str = ".v2-pionex/state.json", client: BinancePublicClient | None = None,
             now_ms: int | None = None) -> dict:
    if symbol not in ALLOWED_SYMBOLS:
        raise ValueError("only_liquid_spot_usdt_symbols_supported")
    if not 20 <= budget <= 10000:
        raise ValueError("budget_out_of_bounds")
    if any(os.getenv(name) for name in ("BINANCE_API_KEY", "BINANCE_API_SECRET",
                                        "V2_BINANCE_API_KEY", "V2_BINANCE_API_SECRET")):
        raise RuntimeError("private_exchange_credentials_must_not_be_present")
    own = client is None
    market = client or BinancePublicClient()
    try:
        info = market.exchange_info(symbol)
        rules = Rules.from_exchange_info(info, symbol)
        candles = market.klines(symbol, "15m", 120)
        hourly = market.klines(symbol, "1h", 120)
        _continuous(candles, BAR_MS, 65)
        _continuous(hourly, HOUR_MS, 55)
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        last_open = int(candles[-1]["open_time"])
        if not BAR_MS <= now - last_open <= 3 * BAR_MS:
            raise RuntimeError("stale_or_future_closed_bar_no_strategy_execution")
        book = market.book_tickers().get(symbol)
        if not book:
            raise RuntimeError("spot_book_unavailable")
        file = Path(path)
        restored = file.is_file()
        if restored:
            data = json.loads(file.read_text())
            if (not isinstance(data, dict) or data.get("schema") != 1 or
                    data.get("symbol") != symbol or
                    data.get("virtual_budget_per_scenario") != budget):
                raise RuntimeError("incompatible_ledger_fail_closed_no_reset")
            states = data.get("strategies")
        else:
            states = {mode: new_state(mode, symbol, budget) for mode in MODES}
        # Validate BEFORE applying a trade. A valid JSON document might still
        # contain phantom PnL, negative cash, mismatched lots or NaN balances.
        validate_ledger(states, symbol=symbol, budget=budget)
        results = {mode: step(states[mode], candles, hourly, book, rules) for mode in MODES}
        # Validate AFTER each simulation too. On failure, leave prior durable
        # state unchanged, rather than persisting an impossible accounting row.
        validate_ledger(states, symbol=symbol, budget=budget)
        report = {"schema": 1, "event": "PIONEX_STYLE_PAPER_SCAN", "symbol": symbol,
                  "last_closed_bar": last_open, "prior_ledger_restored": restored,
                  "virtual_budget_per_scenario": budget,
                  "virtual_scenarios_are_independent_not_additive": True,
                  "exchange_min_notional": rules.min_notional, "strategies": results,
                  "real_orders": 0, "live_trading": False,
                  "status": "FORWARD_SIMULATION_NOT_PROFIT_APPROVAL"}
        file.parent.mkdir(parents=True, exist_ok=True)
        temporary = file.with_suffix(".pending")
        temporary.write_text(json.dumps({"schema": 1, "symbol": symbol,
                                         "virtual_budget_per_scenario": budget,
                                         "strategies": states}, sort_keys=True,
                                        allow_nan=False))
        os.replace(temporary, file)
        return report
    finally:
        if own:
            market.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Pionex-inspired three-mode virtual Spot bot")
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--symbol", default="BTCUSDT", choices=sorted(ALLOWED_SYMBOLS))
    parser.add_argument("--budget", type=float, default=50.0)
    parser.add_argument("--state", default=".v2-pionex/state.json")
    args = parser.parse_args()
    print(json.dumps(run_once(symbol=args.symbol, budget=args.budget, path=args.state),
                     sort_keys=True, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
