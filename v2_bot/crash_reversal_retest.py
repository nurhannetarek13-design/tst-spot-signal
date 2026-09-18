"""Crash -> green reversal -> wick retest -> confirmed break, forward PAPER ONLY.

This candidate is intentionally isolated from the existing nine-account shelf: no
old ledger migrations, no private exchange routes, and no historical fake fills.
Rules are hypotheses, not a demonstrated profitable trading system.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from statistics import fmean
from typing import Any

from .binance_public import BinancePublicClient
from .pionex_run import _continuous
from .pionex_style import FEE, SLIPPAGE, Rules, _buy, _sell, snapshot

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
BAR_MS = 900_000
MODE = "crash_reversal_retest"


def new_state(symbol: str, budget: float = 50.0) -> dict[str, Any]:
    if symbol not in SYMBOLS or not 20 <= budget <= 10_000:
        raise ValueError("invalid_crash_reversal_configuration")
    return {"schema": 1, "mode": MODE, "symbol": symbol, "budget": float(budget),
            "cash": float(budget), "lots": [], "realized_pnl": 0.0,
            "fees_paid": 0.0, "anchor": None, "last_bar": -1, "halted": False,
            "trades": 0, "events": [], "stop": None, "target": None}


def validate(state: dict[str, Any], *, symbol: str, budget: float) -> None:
    """Reject corrupt state rather than resetting or displaying phantom returns."""
    if (not isinstance(state, dict) or state.get("schema") != 1
            or state.get("mode") != MODE or state.get("symbol") != symbol
            or state.get("budget") != budget or type(state.get("halted")) is not bool):
        raise RuntimeError("corrupt_crash_ledger_identity")
    def num(key: str, minimum: float | None = None) -> float:
        value = state.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or (minimum is not None and value < minimum):
            raise RuntimeError(f"corrupt_crash_ledger_{key}")
        return float(value)
    cash, realized = num("cash", -1e-8), num("realized_pnl")
    fees, trades = num("fees_paid", 0), num("trades", 0)
    bar = num("last_bar")
    if not trades.is_integer() or not bar.is_integer() or (bar != -1 and (bar < 0 or int(bar) % BAR_MS)):
        raise RuntimeError("corrupt_crash_ledger_counters")
    lots = state.get("lots")
    if not isinstance(lots, list) or len(lots) > 1 or len(lots) > trades:
        raise RuntimeError("corrupt_crash_ledger_inventory")
    if not isinstance(state.get("events"), list) or len(state["events"]) > 100:
        raise RuntimeError("corrupt_crash_ledger_events")
    cost = 0.0
    for lot in lots:
        if not isinstance(lot, dict):
            raise RuntimeError("corrupt_crash_ledger_lot")
        qty, entry, paid = (lot.get(k) for k in ("qty", "entry", "cost"))
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (qty, entry, paid)):
            raise RuntimeError("corrupt_crash_ledger_lot_values")
        if not math.isclose(paid, qty * entry * (1 + FEE), abs_tol=1e-6, rel_tol=1e-8):
            raise RuntimeError("corrupt_crash_ledger_cost_basis")
        cost += paid
        if fees + 1e-6 < qty * entry * FEE:
            raise RuntimeError("corrupt_crash_ledger_entry_fees")
    if not math.isclose(cash + cost, budget + realized, abs_tol=1e-5, rel_tol=1e-9):
        raise RuntimeError("corrupt_crash_ledger_capital")
    stop, target = state.get("stop"), state.get("target")
    if lots:
        if (type(stop) not in (int, float) or type(target) not in (int, float)
                or not math.isfinite(stop) or not math.isfinite(target)
                or not 0 < stop < lots[0]["entry"] < target):
            raise RuntimeError("corrupt_crash_ledger_protection")
    elif stop is not None or target is not None:
        raise RuntimeError("corrupt_crash_ledger_stale_protection")


def detect(candles: list[dict[str, float]], ask: float, bid: float) -> dict[str, float] | None:
    """Use only last four CLOSED candles; entry at CURRENT book after confirmation.

    - 8-bar decline >= 3%; final crash bar red >= 1.5% and >1.25x pre-crash TR.
    - Subsequent green reversal closes above crash close and in its upper half.
    - Next wick retests >= half reversal body without breaching crash low.
    - Confirmation closes above both reversal and retest highs, vol >=1.2x.
    The fixed parameters are unoptimized research hypotheses. No intrabar orders.
    """
    if len(candles) < 80 or not 0 < bid <= ask or (ask - bid) / bid > 0.003:
        return None
    crash, reversal, retest, confirm = candles[-4:]
    prior = candles[-24:-4]
    atr = fmean(max(float(b["high"]) - float(b["low"]),
                    abs(float(b["high"]) - float(b["close"])),
                    abs(float(b["low"]) - float(b["close"]))) for b in prior)
    if atr <= 0 or any(float(b["low"]) <= 0 for b in (crash, reversal, retest, confirm)):
        return None
    if not (crash["close"] <= candles[-12]["close"] * .97
            and crash["close"] <= crash["open"] * .985
            and crash["high"] - crash["low"] >= 1.25 * atr):
        return None
    if not (reversal["close"] > reversal["open"] and reversal["close"] > crash["close"]
            and reversal["close"] >= (reversal["high"] + reversal["low"]) / 2):
        return None
    body_mid = (reversal["open"] + reversal["close"]) / 2
    lower_wick = min(retest["open"], retest["close"]) - retest["low"]
    if not (crash["low"] < retest["low"] <= body_mid
            and retest["close"] > crash["low"]
            and lower_wick >= abs(retest["close"] - retest["open"]) * .5):
        return None
    if not (confirm["close"] > max(reversal["high"], retest["high"])
            and confirm["close"] > confirm["open"]
            and confirm["quote_volume"] >= 1.2 * fmean(b["quote_volume"] for b in prior)):
        return None
    # No buying a later price spike merely because the last closed candle qualified.
    entry = ask * (1 + SLIPPAGE)
    if ask > confirm["close"] * 1.005:
        return None
    stop = retest["low"] - .15 * atr
    risk = entry - stop
    if not (0.005 <= risk / entry <= .04):
        return None
    target = entry + 1.2 * risk  # relative to the true simulated market entry
    if (target / entry - 1) <= 2 * (FEE + SLIPPAGE) + (ask - bid) / bid + .002:
        return None
    return {"stop": stop, "target": target, "entry": entry, "risk_per_unit": risk}


def _report(state: dict[str, Any], book: dict[str, float], action: str) -> dict[str, Any]:
    return {**snapshot(state, book, action), "stop": state["stop"],
            "target": state["target"], "candidate": True,
            "status": "UNPROVEN_FORWARD_PAPER_ONLY"}


def step(state: dict[str, Any], candles: list[dict[str, float]],
         book: dict[str, float], rules: Rules) -> dict[str, Any]:
    if len(candles) < 80:
        raise RuntimeError("crash_candles_insufficient")
    bid, ask = float(book.get("bid", 0)), float(book.get("ask", 0))
    if not 0 < bid <= ask or (ask - bid) / bid > .003:
        raise RuntimeError("crash_book_invalid_or_wide_spread")
    bar = int(candles[-1]["open_time"])
    if bar <= state["last_bar"]:
        return _report(state, book, "duplicate_bar_no_action")
    if state["last_bar"] > 0 and bar - state["last_bar"] > BAR_MS:
        if state["lots"] or state["halted"]:
            state["halted"] = True
            return _report(state, book, "exposed_gap_halted_no_invented_fill")
        state["last_bar"] = bar
        return _report(state, book, "flat_gap_resynchronized_no_trade")
    state["last_bar"] = bar
    if state["halted"]:
        return _report(state, book, "halted_manual_reconciliation_required")
    equity = state["cash"] + sum(lot["qty"] * bid for lot in state["lots"])
    if equity <= state["budget"] - 2:
        state["halted"] = True
        action = _sell(state, bid, rules, "portfolio_loss_cap") if state["lots"] else "loss_cap_flat"
        if not state["lots"]:
            state["stop"] = state["target"] = None
        return _report(state, book, action + "_halted")
    action = "no_confirmed_crash_retest_signal"
    if state["lots"]:
        stop, target = float(state["stop"]), float(state["target"])
        # A historical low is evidence of a missed stop, NOT proof of a fill.
        # Halt if price recovered: the intrabar execution is unknowable.
        if float(candles[-1]["low"]) <= stop and bid > stop:
            state["halted"] = True
            action = "intrabar_stop_touch_halted_no_fabricated_fill"
        elif bid <= stop:
            action = _sell(state, bid, rules, "observed_stop_market_exit")
            state["halted"] = True
            if not state["lots"]:
                state["stop"] = state["target"] = None
            action += "_halted"
        elif bid >= target:
            action = _sell(state, bid, rules, "observed_target_market_exit")
            if not state["lots"]:
                state["stop"] = state["target"] = None
        else:
            action = "position_open_no_historical_target_fill"
    else:
        signal = detect(candles, ask, bid)
        if signal:
            ticket = max(rules.min_notional * 1.05, min(15., state["budget"] * .30))
            # Independent 50-USDT scenario; no leverage, no martingale, one lot.
            quantity = ticket / signal["entry"]
            worst_planned_risk = quantity * signal["risk_per_unit"] + 2 * ticket * FEE
            if worst_planned_risk > min(2., state["budget"] * .04):
                action = "risk_budget_rejected"
            else:
                action = _buy(state, ask, ticket, rules, "crash_reversal_retest_confirmed")
                if action == "paper_buy":
                    state["stop"], state["target"] = signal["stop"], signal["target"]
    state["events"] = (state["events"] + [{"bar": bar, "action": action}])[-100:]
    return _report(state, book, action)


def state_path(root: str | Path, symbol: str) -> str:
    if symbol not in SYMBOLS:
        raise ValueError("unsupported_crash_symbol")
    return str(Path(root) / f"{symbol.lower()}_crash_reversal.json")


def run_once(symbol: str, *, budget: float = 50., path: str,
             client: BinancePublicClient, now_ms: int | None = None) -> dict[str, Any]:
    if symbol not in SYMBOLS or not 20 <= budget <= 10_000:
        raise ValueError("invalid_crash_run_configuration")
    if any(os.getenv(k) for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "V2_BINANCE_API_KEY", "V2_BINANCE_API_SECRET")):
        raise RuntimeError("private_credentials_prohibited_in_paper_runner")
    rules = Rules.from_exchange_info(client.exchange_info(symbol), symbol)
    candles = client.klines(symbol, "15m", 120)
    _continuous(candles, BAR_MS, 80)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    bar = int(candles[-1]["open_time"])
    if not BAR_MS <= now - bar <= 3 * BAR_MS:
        raise RuntimeError("stale_or_future_crash_candle")
    book = client.book_tickers().get(symbol)
    if not book:
        raise RuntimeError("missing_crash_book")
    file = Path(path)
    restored = file.is_file()
    state = json.loads(file.read_text()) if restored else new_state(symbol, budget)
    validate(state, symbol=symbol, budget=budget)
    result = step(state, candles, book, rules)
    validate(state, symbol=symbol, budget=budget)
    file.parent.mkdir(parents=True, exist_ok=True)
    pending = file.with_suffix(".pending")
    pending.write_text(json.dumps(state, sort_keys=True, allow_nan=False))
    os.replace(pending, file)
    return {"symbol": symbol, "prior_ledger_restored": restored, "last_closed_bar": bar,
            "strategy": result, "virtual_budget_usdt": budget,
            "real_orders": 0, "live_trading": False, "candidate": True}


def run_all(*, budget: float = 50., state_dir: str = ".v2-crash-reversal",
            client: BinancePublicClient | None = None, now_ms: int | None = None) -> dict[str, Any]:
    own = client is None
    market = client or BinancePublicClient()
    reports = {}
    try:
        for symbol in SYMBOLS:
            try:
                reports[symbol] = run_once(symbol, budget=budget,
                    path=state_path(state_dir, symbol), client=market, now_ms=now_ms)
            except Exception as exc:
                raise RuntimeError(f"crash_paper_batch_failed_{symbol}_{type(exc).__name__}") from exc
        if len({row["last_closed_bar"] for row in reports.values()}) != 1:
            raise RuntimeError("crash_symbol_bars_disagree")
        return {"event": "CRASH_REVERSAL_RETEST_PAPER_SCAN", "candidate": True,
                "status": "UNPROVEN_FORWARD_PAPER_ONLY", "symbols": list(SYMBOLS),
                "independent_scenarios_not_pooled_capital": True,
                "virtual_budget_per_scenario": budget, "real_orders": 0,
                "live_trading": False, "by_symbol": reports}
    finally:
        if own:
            market.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated crash-reversal/retest research-paper challenger")
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--budget", type=float, default=50.)
    parser.add_argument("--state-dir", default=".v2-crash-reversal")
    args = parser.parse_args()
    print(json.dumps(run_all(budget=args.budget, state_dir=args.state_dir),
                     sort_keys=True, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
