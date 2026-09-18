"""Same-snapshot A/B paper control: original crash retest vs bullish-FVG retest.

Six INDEPENDENT alternative 50-USDT accounts; they do not share capital and
are not connected to exchange execution. Both modes see IDENTICAL immutable
public Binance candles and bid/ask per pair, captured once per scan. Old nine
strategy accounts and old crash candidate ledgers are never modified.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Any

from . import crash_reversal_retest as baseline
from .binance_public import BinancePublicClient
from .fvg_detector import detect_crash_fvg_retest
from .pionex_run import _continuous
from .pionex_style import FEE, SLIPPAGE, Rules, _buy, _floor, _sell, snapshot

SYMBOLS = baseline.SYMBOLS
BAR_MS = baseline.BAR_MS
MODE = "crash_reversal_fvg_retest"
VARIANTS = ("baseline", "fvg")


def new_state(symbol: str, budget: float = 50.) -> dict[str, Any]:
    state = baseline.new_state(symbol, budget)
    state["mode"] = MODE
    state["last_signal"] = None
    return state


def validate(state: dict[str, Any], *, symbol: str, budget: float) -> None:
    if not isinstance(state, dict) or state.get("mode") != MODE:
        raise RuntimeError("invalid_fvg_variant_identity")
    # Use exactly the existing independently audited conservation/protection
    # invariants, changing the validation VIEW only, never the persistent mode.
    baseline.validate({**state, "mode": baseline.MODE}, symbol=symbol, budget=budget)
    signal = state.get("last_signal")
    if signal is not None:
        if not isinstance(signal, dict) or set(signal) != {
                "formed_bar", "retest_bar", "confirm_bar", "lower", "upper",
                "entry", "stop", "target"}:
            raise RuntimeError("invalid_fvg_signal_audit")
        if not all(type(value) in (int, float) and math.isfinite(value)
                   for value in signal.values()):
            raise RuntimeError("invalid_fvg_signal_numbers")
        if not (0 < signal["lower"] < signal["upper"] < signal["entry"] < signal["target"]
                and 0 < signal["stop"] < signal["entry"]
                and signal["formed_bar"] < signal["retest_bar"] < signal["confirm_bar"]):
            raise RuntimeError("invalid_fvg_signal_order")


def report(state: dict[str, Any], book: dict[str, float], action: str) -> dict[str, Any]:
    return {**snapshot(state, book, action), "stop": state["stop"],
            "target": state["target"], "last_signal": state["last_signal"],
            "status": "UNPROVEN_FVG_FORWARD_PAPER_ONLY"}


def step(state: dict[str, Any], candles: list[dict[str, float]],
         book: dict[str, float], rules: Rules) -> dict[str, Any]:
    if len(candles) < 80:
        raise RuntimeError("insufficient_closed_fvg_candles")
    bid, ask = float(book.get("bid", 0)), float(book.get("ask", 0))
    if not all(math.isfinite(x) for x in (bid, ask)) or not (0 < bid <= ask and (ask-bid)/bid <= .003):
        raise RuntimeError("invalid_fvg_book")
    bar = int(candles[-1]["open_time"])
    if bar <= state["last_bar"]:
        return report(state, book, "duplicate_bar_no_action")
    if state["last_bar"] > 0 and bar - state["last_bar"] > BAR_MS:
        if state["lots"] or state["halted"]:
            state["halted"] = True
            return report(state, book, "exposed_gap_halted_no_invented_fill")
        state["last_bar"] = bar
        state["events"] = (state["events"] + [{"bar": bar,
            "action": "flat_gap_resynchronized_no_trade"}])[-100:]
        return report(state, book, "flat_gap_resynchronized_no_trade")
    state["last_bar"] = bar
    if state["halted"]:
        return report(state, book, "halted_manual_reconciliation_required")
    equity = state["cash"] + sum(lot["qty"] * bid for lot in state["lots"])
    if equity <= state["budget"] - 2.:
        state["halted"] = True
        action = _sell(state, bid, rules, "observed_portfolio_loss_limit") if state["lots"] else "loss_limit_flat"
        if not state["lots"]:
            state["stop"] = state["target"] = None
        state["events"] = (state["events"] + [{"bar": bar, "action": action,
            "bid": bid, "equity": equity, "reason": "observed_loss_not_guaranteed_maximum"}])[-100:]
        return report(state, book, action + "_halted")
    action = "no_confirmed_crash_fvg_retest_signal"
    record: dict[str, Any] = {"bar": bar, "action": action,
                               "bid": bid, "ask": ask, "symbol": state["symbol"]}
    if state["lots"]:
        stop, target = float(state["stop"]), float(state["target"])
        record.update({"stop": stop, "target": target})
        # A touched intrabar stop followed by recovery is NOT a verified fill.
        if float(candles[-1]["low"]) <= stop and bid > stop:
            state["halted"] = True
            action = "intrabar_stop_touch_halted_no_fabricated_fill"
        elif bid <= stop:
            old_realized = state["realized_pnl"]
            action = _sell(state, bid, rules, "observed_stop_market_exit")
            state["halted"] = True
            if not state["lots"]:
                state["stop"] = state["target"] = None
                record["closed_trade_net_pnl_usdt"] = state["realized_pnl"] - old_realized
            action += "_halted"
        elif bid >= target:
            old_realized = state["realized_pnl"]
            action = _sell(state, bid, rules, "observed_target_market_exit")
            if not state["lots"]:
                state["stop"] = state["target"] = None
                record["closed_trade_net_pnl_usdt"] = state["realized_pnl"] - old_realized
        else:
            action = "position_open_no_historical_target_fill"
    else:
        signal = detect_crash_fvg_retest(candles, ask=ask, bid=bid)
        if signal:
            record["signal"] = {k: signal[k] for k in (
                "formed_bar", "retest_bar", "confirm_bar", "lower", "upper",
                "entry", "stop", "target", "atr", "gap_pct")}
            ticket = max(rules.min_notional * 1.05,
                         min(15., state["budget"] * .30))
            qty = _floor(ticket / signal["entry"], rules.step_size)
            projected_exit = qty * signal["stop"] * (1 - SLIPPAGE)
            # The planned stop must itself satisfy exchange lot/min-notional.
            # The market can still gap through it; this is NOT a guaranteed cap.
            if qty < rules.min_qty or projected_exit < rules.min_notional * 1.01:
                action = "stop_exit_would_violate_exchange_minimum"
            else:
                theoretical_risk = (qty * (signal["entry"] - signal["stop"] * (1-SLIPPAGE))
                                    + qty * (signal["entry"] + signal["stop"]) * FEE)
                if theoretical_risk > min(2., state["budget"] * .04):
                    action = "risk_budget_rejected"
                else:
                    action = _buy(state, ask, ticket, rules,
                                  "crash_reversal_fvg_retest_confirmed")
                    if action == "paper_buy":
                        state["stop"], state["target"] = signal["stop"], signal["target"]
                        state["last_signal"] = {k: signal[k] for k in (
                            "formed_bar", "retest_bar", "confirm_bar", "lower", "upper",
                            "entry", "stop", "target")}
                        record["virtual_fill_entry"] = state["lots"][0]["entry"]
                        record["virtual_fill_qty"] = state["lots"][0]["qty"]
                        record["entry_fee_usdt"] = state["lots"][0]["cost"] - (
                            state["lots"][0]["entry"] * state["lots"][0]["qty"])
                        record["planned_risk_usdt_not_guaranteed"] = theoretical_risk
    record["action"] = action
    state["events"] = (state["events"] + [record])[-100:]
    return report(state, book, action)


def state_path(root: str | Path, symbol: str, variant: str) -> str:
    if symbol not in SYMBOLS or variant not in VARIANTS:
        raise ValueError("invalid_fvg_ab_ledger_path")
    return str(Path(root) / f"{symbol.lower()}_{variant}.json")


class FrozenPublicMarket:
    """Exactly the same immutable public snapshot for baseline and challenger."""
    def __init__(self, info: dict[str, Any], candles: list[dict[str, float]],
                 book: dict[str, float], symbol: str):
        self._info, self._candles = info, candles
        self._book, self._symbol = dict(book), symbol

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        if symbol != self._symbol:
            raise RuntimeError("frozen_market_symbol_mismatch")
        return self._info

    def klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, float]]:
        if symbol != self._symbol or interval != "15m":
            raise RuntimeError("frozen_market_mismatched_request")
        return self._candles

    def book_tickers(self) -> dict[str, dict[str, float]]:
        return {self._symbol: dict(self._book)}


def _persist(path: str, state: dict[str, Any]) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    pending = file.with_suffix(".pending")
    pending.write_text(json.dumps(state, sort_keys=True, allow_nan=False))
    os.replace(pending, file)


def run_all(*, budget: float = 50., state_dir: str = ".v2-fvg-ab",
            client: BinancePublicClient | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
    if not 20 <= budget <= 10_000:
        raise ValueError("invalid_fvg_ab_budget")
    if any(os.getenv(k) for k in (
            "BINANCE_API_KEY", "BINANCE_API_SECRET", "V2_BINANCE_API_KEY", "V2_BINANCE_API_SECRET")):
        raise RuntimeError("private_exchange_credentials_forbidden")
    paths = [Path(state_path(state_dir, symbol, mode))
             for symbol in SYMBOLS for mode in VARIANTS]
    if any(p.exists() for p in paths) and not all(p.is_file() for p in paths):
        raise RuntimeError("partial_fvg_ab_state_fail_closed_no_reset")
    own = client is None
    market = client or BinancePublicClient()
    reports: dict[str, Any] = {}
    now = int(time.time()*1000) if now_ms is None else now_ms
    try:
        # Capture and validate ALL symbols BEFORE either variant mutates state.
        frozen = {}
        for symbol in SYMBOLS:
            info = market.exchange_info(symbol)
            rules = Rules.from_exchange_info(info, symbol)
            candles = market.klines(symbol, "15m", 120)
            _continuous(candles, BAR_MS, 80)
            bar = int(candles[-1]["open_time"])
            if not BAR_MS <= now - bar <= 3*BAR_MS:
                raise RuntimeError(f"stale_fvg_ab_data_{symbol}")
            book = market.book_tickers().get(symbol)
            if book is None:
                raise RuntimeError(f"missing_fvg_ab_book_{symbol}")
            if not (0 < float(book["bid"]) <= float(book["ask"])):
                raise RuntimeError(f"invalid_fvg_ab_book_{symbol}")
            frozen[symbol] = (FrozenPublicMarket(info, candles, book, symbol), rules, bar)
        if len({row[2] for row in frozen.values()}) != 1:
            raise RuntimeError("fvg_ab_symbols_not_same_closed_bar")
        # Verify EVERY existing ledger before any strategy step is committed.
        for symbol in SYMBOLS:
            original = Path(state_path(state_dir, symbol, "baseline"))
            alternate = Path(state_path(state_dir, symbol, "fvg"))
            if original.is_file():
                baseline.validate(json.loads(original.read_text()), symbol=symbol, budget=budget)
                validate(json.loads(alternate.read_text()), symbol=symbol, budget=budget)
        for symbol in SYMBOLS:
            snapshot_market, rules, bar = frozen[symbol]
            base = baseline.run_once(symbol, budget=budget,
                    path=state_path(state_dir, symbol, "baseline"),
                    client=snapshot_market, now_ms=now)
            file = Path(state_path(state_dir, symbol, "fvg"))
            restored = file.is_file()
            state = json.loads(file.read_text()) if restored else new_state(symbol, budget)
            validate(state, symbol=symbol, budget=budget)
            result = step(state, snapshot_market.klines(symbol, "15m", 120),
                          snapshot_market.book_tickers()[symbol], rules)
            validate(state, symbol=symbol, budget=budget)
            _persist(str(file), state)
            control, challenger = base["strategy"], result
            reports[symbol] = {"last_closed_bar": bar,
                "both_variants_restored": base["prior_ledger_restored"] and restored,
                "baseline": control, "fvg": challenger,
                "descriptive_pnl_difference_usdt_not_edge": round(
                    challenger["total_pnl_including_unrealized_usdt"] -
                    control["total_pnl_including_unrealized_usdt"], 6)}
        return {"event": "FVG_VS_CRASH_BASELINE_SAME_SNAPSHOT_FORWARD_PAPER",
            "status": "UNPROVEN_PAPER_COMPARISON_NOT_PROFIT_APPROVAL",
            "last_closed_bar": next(iter(frozen.values()))[2],
            "symbols": list(SYMBOLS), "modes": list(VARIANTS),
            "six_independent_alternative_scenarios_do_not_sum_capital": True,
            "virtual_budget_per_scenario": budget, "real_orders": 0,
            "live_trading": False, "by_symbol": reports}
    finally:
        if own:
            market.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Public Spot FVG A/B forward paper, no live routing")
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--budget", type=float, default=50.)
    parser.add_argument("--state-dir", default=".v2-fvg-ab")
    args = parser.parse_args()
    print(json.dumps(run_all(budget=args.budget, state_dir=args.state_dir),
                     sort_keys=True, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
