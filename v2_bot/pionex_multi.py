"""Three-symbol controller for the credential-free Pionex-style virtual shelf.

Each (symbol, strategy) is an INDEPENDENT hypothetical account. Never total
these balances or call the results a funded trading portfolio. The legacy BTC
state path is preserved; ETH and SOL get separate state files. Any individual
failure aborts the batch instead of claiming a successful nine-account scan.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .binance_public import BinancePublicClient
from .pionex_run import run_once
from .pionex_style import MODES

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def state_path(root: str | Path, symbol: str) -> str:
    if symbol not in SYMBOLS:
        raise ValueError("unsupported_symbol")
    directory = Path(root)
    return str(directory / ("state.json" if symbol == "BTCUSDT" else f"{symbol.lower()}_state.json"))


def run_all(*, budget: float = 50.0, state_dir: str = ".v2-pionex",
            client: BinancePublicClient | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
    """Run every symbol, fail closed on any error, report only non-pooled PnL.

    Individual ledgers are atomically persisted by run_once. On a batch error,
    some may have advanced; reruns deduplicate their candle and the others
    catch up or halt on a gap. We never roll back a simulated fill or erase a
    previous ledger to make a report look successful.
    """
    own = client is None
    market = client or BinancePublicClient()
    reports: dict[str, dict[str, Any]] = {}
    try:
        for symbol in SYMBOLS:
            try:
                result = run_once(symbol=symbol, budget=budget,
                                  path=state_path(state_dir, symbol),
                                  client=market, now_ms=now_ms)
            except Exception as exc:
                raise RuntimeError(f"paper_batch_failed_at_{symbol}_{type(exc).__name__}") from exc
            if (result.get("real_orders") != 0 or result.get("live_trading") is not False
                    or result.get("virtual_budget_per_scenario") != budget
                    or set(result.get("strategies", {})) != set(MODES)):
                raise RuntimeError(f"invalid_virtual_report_{symbol}")
            reports[symbol] = result
        bar_times = {row["last_closed_bar"] for row in reports.values()}
        if len(bar_times) != 1:
            raise RuntimeError("symbol_candle_times_disagree_fail_closed")
        return {
            "event": "PIONEX_STYLE_MULTI_SYMBOL_PAPER_SCAN",
            "status": "FORWARD_SIMULATION_NOT_PROFIT_APPROVAL",
            "symbols": list(SYMBOLS),
            "virtual_scenarios": len(SYMBOLS) * len(MODES),
            "virtual_budget_per_scenario": budget,
            "independent_scenarios_not_a_pooled_portfolio": True,
            "do_not_sum_hypothetical_balances": True,
            "real_orders": 0,
            "live_trading": False,
            "last_closed_bar": reports[SYMBOLS[0]]["last_closed_bar"],
            "by_symbol": reports,
        }
    finally:
        if own:
            market.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Nine independent virtual Spot scenarios; never live")
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--budget", type=float, default=50.0)
    parser.add_argument("--state-dir", default=".v2-pionex")
    args = parser.parse_args()
    print(json.dumps(run_all(budget=args.budget, state_dir=args.state_dir),
                     sort_keys=True, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
