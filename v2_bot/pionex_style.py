"""Credential-free Spot strategy shelf: geometric grid, fixed-size DCA, breakout.

Every mode owns its own VIRTUAL 50-USDT scenario; balances must never be summed or
mistaken for exchange holdings. No private Binance API client is imported here.
This is executable forward paper simulation, NOT a proven edge or live execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from statistics import fmean
from typing import Any

MODES = ("spot_grid", "fixed_dca", "trend_breakout")
FEE = 0.001
SLIPPAGE = 0.0005


@dataclass(frozen=True)
class Rules:
    min_notional: float
    min_qty: float
    step_size: float

    @classmethod
    def from_exchange_info(cls, info: dict[str, Any], symbol: str) -> "Rules":
        rows = [row for row in info.get("symbols", []) if row.get("symbol") == symbol]
        if len(rows) != 1 or rows[0].get("status") != "TRADING" or not rows[0].get("isSpotTradingAllowed", True):
            raise ValueError("spot_symbol_not_trading")
        filters = {f["filterType"]: f for f in rows[0].get("filters", [])}
        lot = filters.get("LOT_SIZE")
        if not lot or not float(lot.get("stepSize", 0)) or not float(lot.get("minQty", 0)):
            raise ValueError("invalid_lot_filter")
        minimum = max(float(filters.get(k, {}).get("minNotional", 0)) for k in ("NOTIONAL", "MIN_NOTIONAL"))
        if minimum <= 0:
            raise ValueError("minimum_notional_unknown")
        return cls(minimum, float(lot["minQty"]), float(lot["stepSize"]))


def _floor(qty: float, step: float) -> float:
    return float((Decimal(str(qty)) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN) * Decimal(str(step)))


def new_state(mode: str, symbol: str, budget: float = 50.0) -> dict[str, Any]:
    if mode not in MODES or not symbol.endswith("USDT") or budget <= 0:
        raise ValueError("invalid_strategy_configuration")
    return {"schema": 1, "mode": mode, "symbol": symbol, "budget": float(budget),
            "cash": float(budget), "lots": [], "realized_pnl": 0.0,
            "fees_paid": 0.0, "anchor": None, "last_bar": -1, "halted": False,
            "trades": 0, "events": []}


def _ema(values: list[float], period: int) -> float:
    result = fmean(values[:period])
    alpha = 2.0 / (period + 1)
    for value in values[period:]:
        result += alpha * (value - result)
    return result


def _buy(state: dict[str, Any], ask: float, quote: float, rules: Rules, reason: str) -> str:
    price = ask * (1 + SLIPPAGE)
    if price <= 0 or quote <= 0:
        return "invalid_buy_price"
    qty = _floor(quote / price, rules.step_size)
    notional = qty * price
    fee = notional * FEE
    if qty < rules.min_qty or notional + 1e-9 < rules.min_notional:
        return "buy_under_exchange_minimum"
    if notional + fee > state["cash"] + 1e-9:
        return "insufficient_virtual_cash"
    state["cash"] -= notional + fee
    state["lots"].append({"qty": qty, "cost": notional + fee, "entry": price, "reason": reason})
    state["fees_paid"] += fee
    state["trades"] += 1
    return "paper_buy"


def _sell(state: dict[str, Any], bid: float, rules: Rules, reason: str, index: int | None = None) -> str:
    lots = state["lots"] if index is None else [state["lots"][index]]
    qty = _floor(sum(lot["qty"] for lot in lots), rules.step_size)
    price = bid * (1 - SLIPPAGE)
    notional = qty * price
    if qty < rules.min_qty or notional + 1e-9 < rules.min_notional:
        return "sell_under_exchange_minimum_do_not_fake_fill"
    cost = sum(lot["cost"] for lot in lots)
    fee = notional * FEE
    state["cash"] += notional - fee
    state["realized_pnl"] += notional - fee - cost
    state["fees_paid"] += fee
    state["trades"] += 1
    if index is None:
        state["lots"] = []
    else:
        state["lots"].pop(index)
    return "paper_sell"


def step(state: dict[str, Any], candles: list[dict[str, float]], hourly: list[dict[str, float]],
         book: dict[str, float], rules: Rules) -> dict[str, Any]:
    """One fill decision per CLOSED 15m bar at CURRENT market book; never replay fills.

    The source candle is already closed. No high/low-derived fantasy fills, no
    same-candle round trips. Repeated bars are strictly idempotent.
    """
    if state.get("schema") != 1 or state.get("mode") not in MODES:
        raise ValueError("unknown_strategy_state_version")
    if len(candles) < 65 or len(hourly) < 55:
        raise ValueError("not_enough_closed_candles")
    bar = int(candles[-1]["open_time"])
    if bar <= int(state["last_bar"]):
        return snapshot(state, book, "duplicate_bar_no_action")
    if state["last_bar"] > 0 and bar - int(state["last_bar"]) > 2 * 900_000:
        state["halted"] = True
        return snapshot(state, book, "missed_candles_halted_reconciliation_required")
    ask, bid = float(book.get("ask", 0)), float(book.get("bid", 0))
    if not (0 < bid <= ask and (ask - bid) / bid <= 0.003):
        raise ValueError("invalid_or_wide_spread_market_book")
    if any(float(x["close"]) <= 0 for x in candles[-65:] + hourly[-55:]):
        raise ValueError("invalid_market_candles")
    state["last_bar"] = bar
    closes = [float(x["close"]) for x in candles]
    h = [float(x["close"]) for x in hourly]
    bullish = _ema(h, 20) > _ema(h, 50) and h[-1] > _ema(h, 20)
    current = closes[-1]
    highs = [float(x["high"]) for x in candles[-49:-1]]
    lows = [float(x["low"]) for x in candles[-49:-1]]
    ret_range = (max(highs) - min(lows)) / current
    flat = abs(_ema(h, 20) / _ema(h, 50) - 1) < 0.008 and 0.025 <= ret_range <= 0.11
    equity = state["cash"] + sum(lot["qty"] * bid for lot in state["lots"])
    action = "hold"
    if state["halted"]:
        return snapshot(state, book, "halted_manual_reconciliation_required")
    # Portfolio drawdown measured on TOTAL value, never just completed grid cycles.
    if equity <= state["budget"] - 2.0:
        state["halted"] = True
        action = _sell(state, bid, rules, "portfolio_loss_cap") if state["lots"] else "loss_cap_no_position"
        return snapshot(state, book, action + "_halted")
    mode = state["mode"]
    min_ticket = rules.min_notional * 1.05  # quantity rounding can still block
    ticket = max(min_ticket, min(15.0, state["budget"] * 0.30))
    if ticket * 2 + ticket * FEE * 2 > state["budget"]:
        return snapshot(state, book, "insufficient_budget_for_two_safe_tranches")
    if mode == "spot_grid":
        if state["anchor"] is None:
            if flat and bullish:
                state["anchor"] = current
                action = "grid_initialized_without_order"
            else:
                action = "grid_waiting_for_range_and_trend"
        else:
            anchor = float(state["anchor"])
            spacing = max(0.012, 4 * FEE + 2 * (ask - bid) / bid)
            if current < anchor * 0.93 or not bullish:
                state["halted"] = True
                action = _sell(state, bid, rules, "grid_regime_exit") if state["lots"] else "grid_regime_exit_flat"
                action += "_halted"
            else:
                profit_lot = next((i for i, lot in enumerate(state["lots"])
                                   if bid >= lot["entry"] * (1 + spacing)), None)
                if profit_lot is not None:
                    action = _sell(state, bid, rules, "grid_take_profit", profit_lot)
                elif len(state["lots"]) < 2:
                    level = anchor * (1 - spacing * (len(state["lots"]) + 1))
                    if ask <= level and ask >= anchor * 0.93:
                        action = _buy(state, ask, ticket, rules, "grid_lower_rung")
    elif mode == "fixed_dca":
        lots = state["lots"]
        if lots:
            qty = sum(lot["qty"] for lot in lots)
            break_even = sum(lot["cost"] for lot in lots) / qty
            if bid >= break_even * 1.025:
                action = _sell(state, bid, rules, "dca_profit_exit")
            elif bid <= break_even * 0.96 or not bullish:
                action = _sell(state, bid, rules, "dca_stop_or_regime_exit")
            elif len(lots) == 1 and bullish and ask <= lots[0]["entry"] * 0.975:
                action = _buy(state, ask, ticket, rules, "fixed_size_second_tranche")
        elif bullish and 0.015 <= (max(highs) / current - 1) <= 0.045:
            action = _buy(state, ask, ticket, rules, "first_pullback_tranche")
    else:  # trend_breakout
        if state["lots"]:
            entry = state["lots"][0]["entry"]
            if bid <= entry * 0.987 or not bullish:
                action = _sell(state, bid, rules, "trend_stop_or_regime_exit")
            elif bid >= entry * 1.028:
                action = _sell(state, bid, rules, "trend_take_profit")
        else:
            prior_high = max(float(x["high"]) for x in candles[-21:-1])
            prior_vol = fmean(float(x["quote_volume"]) for x in candles[-21:-1])
            volx = float(candles[-1]["quote_volume"]) / max(prior_vol, 1e-12)
            taker = float(candles[-1]["taker_buy_quote"]) / max(float(candles[-1]["quote_volume"]), 1e-12)
            if bullish and current > prior_high and volx >= 1.5 and taker >= 0.56:
                action = _buy(state, ask, ticket, rules, "confirmed_trend_breakout")
    state["events"] = (state.get("events", []) + [{"bar": bar, "action": action}])[-100:]
    return snapshot(state, book, action)


def snapshot(state: dict[str, Any], book: dict[str, float], action: str) -> dict[str, Any]:
    bid = max(0.0, float(book.get("bid", 0)))
    inventory = sum(float(lot["qty"]) for lot in state["lots"])
    equity = float(state["cash"]) + inventory * bid
    return {"mode": state["mode"], "symbol": state["symbol"], "action": action,
            "halted": bool(state["halted"]), "virtual_budget_usdt": state["budget"],
            "virtual_cash_usdt": round(state["cash"], 6), "inventory_qty": inventory,
            "equity_usdt": round(equity, 6),
            "total_pnl_including_unrealized_usdt": round(equity - state["budget"], 6),
            "realized_pnl_after_fees_usdt": round(state["realized_pnl"], 6),
            "fees_paid_usdt": round(state["fees_paid"], 6), "trades": state["trades"],
            "last_closed_bar": state["last_bar"], "live_trading": False,
            "actual_orders": 0, "capital_scenarios_are_independent": True}
