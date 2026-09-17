from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .binance_public import BinancePublicClient

STRATEGY_ID = "external_4h_sma200"
SYMBOL = "BTCUSDT"
SMA_PERIOD = 200
FEE_RATE = 0.001
SLIPPAGE_BPS = 5.0


def _default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "strategy_id": STRATEGY_ID,
        "position": None,
        "closed": 0,
        "wins": 0,
        "losses": 0,
        "gross_profit_usdt": 0.0,
        "gross_loss_abs_usdt": 0.0,
        "net_pnl_usdt": 0.0,
        "equity_usdt": 0.0,
        "equity_peak_usdt": 0.0,
        "max_drawdown_usdt": 0.0,
        "last_evaluated_open_time": 0.0,
        "last_event": None,
    }


def _load_state(path: Path) -> dict[str, Any]:
    state = _default_state()
    if not path.exists():
        return state
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return state
    if not isinstance(payload, dict) or payload.get("strategy_id") != STRATEGY_ID:
        return state
    for key in state:
        if key in payload:
            state[key] = payload[key]
    return state


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n")


def _market_snapshot() -> tuple[list[dict[str, float]], float, float]:
    client = BinancePublicClient(timeout=15.0)
    try:
        candles = client.klines(SYMBOL, "4h", 1000, closed_only=True)
        if len(candles) < SMA_PERIOD + 1:
            raise RuntimeError("insufficient_closed_4h_candles")
        book = client.book_tickers().get(SYMBOL, {})
        ask = float(book.get("ask", 0.0) or 0.0)
        bid = float(book.get("bid", 0.0) or 0.0)
        if ask <= 0 or bid <= 0:
            raise RuntimeError("invalid_book_ticker")
        return candles, ask, bid
    finally:
        client.close()


def _sma(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError("sma_not_ready")
    return sum(values[-period:]) / period


def _stats(state: dict[str, Any]) -> dict[str, Any]:
    closed = int(state["closed"])
    gp = float(state["gross_profit_usdt"])
    gl = float(state["gross_loss_abs_usdt"])
    net = float(state["net_pnl_usdt"])
    return {
        "closed": closed,
        "wins": int(state["wins"]),
        "losses": int(state["losses"]),
        "win_rate": (float(state["wins"]) / closed) if closed else None,
        "profit_factor": (gp / gl) if gl > 0 else None,
        "expectancy_usdt": (net / closed) if closed else None,
        "net_pnl_usdt": net,
        "max_drawdown_usdt": float(state["max_drawdown_usdt"]),
        "open": state["position"] is not None,
    }


def run_once(state_path: Path, quote_size_usdt: float) -> dict[str, Any]:
    if quote_size_usdt <= 0 or quote_size_usdt > 10.0:
        raise ValueError("quote_size_must_be_between_0_and_10_usdt")

    state = _load_state(state_path)
    candles, ask, bid = _market_snapshot()
    last = candles[-1]
    prev = candles[-2]
    open_time = float(last["open_time"])

    closes = [float(row["close"]) for row in candles]
    sma_now = _sma(closes, SMA_PERIOD)
    sma_prev = _sma(closes[:-1], SMA_PERIOD)
    close_now = closes[-1]
    close_prev = closes[-2]
    crossed_above = close_prev <= sma_prev and close_now > sma_now
    crossed_below = close_prev >= sma_prev and close_now < sma_now

    event = None
    if open_time > float(state["last_evaluated_open_time"]):
        position = state.get("position")
        slip = SLIPPAGE_BPS / 10_000.0

        if isinstance(position, dict) and crossed_below:
            exit_price = bid * (1.0 - slip)
            entry_price = float(position["entry_price"])
            quantity = float(position["quantity"])
            gross = (exit_price - entry_price) * quantity
            fees = (entry_price + exit_price) * quantity * FEE_RATE
            pnl = gross - fees
            state["closed"] = int(state["closed"]) + 1
            if pnl > 0:
                state["wins"] = int(state["wins"]) + 1
                state["gross_profit_usdt"] = float(state["gross_profit_usdt"]) + pnl
            else:
                state["losses"] = int(state["losses"]) + 1
                state["gross_loss_abs_usdt"] = float(state["gross_loss_abs_usdt"]) + abs(pnl)
            state["net_pnl_usdt"] = float(state["net_pnl_usdt"]) + pnl
            state["equity_usdt"] = float(state["equity_usdt"]) + pnl
            state["equity_peak_usdt"] = max(float(state["equity_peak_usdt"]), float(state["equity_usdt"]))
            dd = float(state["equity_peak_usdt"]) - float(state["equity_usdt"])
            state["max_drawdown_usdt"] = max(float(state["max_drawdown_usdt"]), dd)
            event = {
                "type": "shadow_close",
                "reason": "close_below_sma200",
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_usdt_net_fees_slippage": round(pnl, 8),
            }
            state["position"] = None

        elif position is None and crossed_above:
            entry_price = ask * (1.0 + slip)
            quantity = quote_size_usdt / entry_price
            state["position"] = {
                "entry_price": entry_price,
                "quantity": quantity,
                "quote_size_usdt": quote_size_usdt,
                "opened_at": open_time,
            }
            event = {
                "type": "shadow_open",
                "reason": "close_cross_above_sma200",
                "entry_price": entry_price,
                "quote_size_usdt": quote_size_usdt,
            }

        state["last_evaluated_open_time"] = open_time
        state["last_event"] = event
        _save_state(state_path, state)

    return {
        "runner": "github_actions_shadow",
        "strategy_id": STRATEGY_ID,
        "symbol": SYMBOL,
        "timeframe": "4h",
        "research_only": True,
        "promotion_eligible": False,
        "live_eligible": False,
        "live_trading": False,
        "signal": {
            "open_time": open_time,
            "close": close_now,
            "previous_close": close_prev,
            "sma200": sma_now,
            "previous_sma200": sma_prev,
            "crossed_above": crossed_above,
            "crossed_below": crossed_below,
            "long_regime": close_now > sma_now,
        },
        "event": event,
        "position": state.get("position"),
        "stats": _stats(state),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=".runtime/btc_4h_sma200_state.json")
    parser.add_argument("--quote-size", type=float, default=10.0)
    args = parser.parse_args()
    print(json.dumps(run_once(Path(args.state), args.quote_size), sort_keys=True))


if __name__ == "__main__":
    main()
