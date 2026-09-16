from __future__ import annotations

import json
from dataclasses import dataclass
from math import isfinite
from typing import Any, Iterable

import httpx


STRATEGY_ID = "btc_ema600_funding"
SYMBOL = "BTCUSDT"
EMA_PERIOD = 600
EXIT_THRESHOLD = 0.02
FUNDING_MAX_PCT = 55.0
TRAILING_STOP_PCT = 0.15
FEE_RATE = 0.001
SLIPPAGE_BPS = 5.0
STATE_META_KEY = "external_edge_btc_ema600_funding_v1"
FUTURES_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"


@dataclass(frozen=True)
class EdgeSnapshot:
    signal_open_time: float
    close: float
    ema: float
    previous_close: float
    previous_ema: float
    crossed_above: bool
    crossed_below_exit: bool
    funding_3d: float | None
    funding_percentile: float | None
    funding_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": STRATEGY_ID,
            "symbol": SYMBOL,
            "signal_open_time": self.signal_open_time,
            "close": self.close,
            "ema": self.ema,
            "previous_close": self.previous_close,
            "previous_ema": self.previous_ema,
            "crossed_above": self.crossed_above,
            "crossed_below_exit": self.crossed_below_exit,
            "funding_3d": self.funding_3d,
            "funding_percentile": self.funding_percentile,
            "funding_allowed": self.funding_allowed,
        }


def _f(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if isfinite(result) else default


def ema_series(values: Iterable[float], period: int = EMA_PERIOD) -> list[float | None]:
    vals = [float(v) for v in values]
    if period <= 1:
        return vals
    out: list[float | None] = [None] * len(vals)
    if len(vals) < period:
        return out
    seed = sum(vals[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    previous = seed
    for idx in range(period, len(vals)):
        previous = (vals[idx] - previous) * alpha + previous
        out[idx] = previous
    return out


def funding_percentile(
    rows: Iterable[dict[str, Any]],
    *,
    as_of_ms: float | None = None,
) -> tuple[float | None, float | None]:
    """Replicate the published 3d-mean / 180d-percentile funding filter.

    Funding is public USD-M perpetual data used only as a signal input.  The bot
    remains Spot-only and never opens a futures position.
    """
    ordered = sorted(
        (
            (int(_f(row.get("fundingTime"))), _f(row.get("fundingRate")))
            for row in rows
            if isinstance(row, dict)
        ),
        key=lambda item: item[0],
    )
    ordered = [(timestamp, rate) for timestamp, rate in ordered if timestamp > 0]
    if len(ordered) < 4:
        return None, None

    end_ms = int(as_of_ms) if as_of_ms and as_of_ms > 0 else ordered[-1][0]
    hour_ms = 3_600_000
    start_ms = end_ms - (180 * 24 + 72 + 24) * hour_ms
    relevant = [(ts, rate) for ts, rate in ordered if ts <= end_ms]
    if not relevant:
        return None, None

    before = [(ts, rate) for ts, rate in relevant if ts < start_ms]
    events = [(ts, rate) for ts, rate in relevant if ts >= start_ms]
    if before:
        events.insert(0, before[-1])
    if not events:
        return None, None

    first_hour = (start_ms // hour_ms) * hour_ms
    last_hour = (end_ms // hour_ms) * hour_ms
    hourly: list[float] = []
    event_idx = 0
    active_rate: float | None = None
    for timestamp in range(first_hour, last_hour + 1, hour_ms):
        while event_idx < len(events) and events[event_idx][0] <= timestamp:
            active_rate = events[event_idx][1]
            event_idx += 1
        if active_rate is not None:
            hourly.append(active_rate)

    if len(hourly) < 24:
        return None, None

    rolling_3d: list[float] = []
    running = 0.0
    for idx, value in enumerate(hourly):
        running += value
        if idx >= 72:
            running -= hourly[idx - 72]
        window_len = min(idx + 1, 72)
        if window_len >= 24:
            rolling_3d.append(running / window_len)

    if not rolling_3d:
        return None, None
    current = rolling_3d[-1]
    history = rolling_3d[-(180 * 24):]
    if len(history) < 30 * 24:
        return current, None

    less = sum(1 for value in history if value < current)
    equal = sum(1 for value in history if value == current)
    percentile = 100.0 * (less + 0.5 * equal) / len(history)
    return current, percentile


def evaluate_snapshot(
    candles: list[dict[str, Any]],
    funding_rows: Iterable[dict[str, Any]],
) -> EdgeSnapshot:
    if len(candles) < EMA_PERIOD + 1:
        raise ValueError("btc_ema600_requires_at_least_601_closed_hourly_candles")

    closes = [_f(row.get("close")) for row in candles]
    if any(value <= 0 for value in closes[-EMA_PERIOD - 1 :]):
        raise ValueError("invalid_btc_close")

    emas = ema_series(closes, EMA_PERIOD)
    ema_now = emas[-1]
    ema_prev = emas[-2]
    if ema_now is None or ema_prev is None:
        raise ValueError("ema_not_ready")

    close_now = closes[-1]
    close_prev = closes[-2]
    crossed_above = close_prev <= ema_prev and close_now > ema_now

    exit_now = ema_now * (1.0 - EXIT_THRESHOLD)
    exit_prev = ema_prev * (1.0 - EXIT_THRESHOLD)
    crossed_below_exit = close_prev >= exit_prev and close_now < exit_now

    funding_3d, funding_pct = funding_percentile(
        funding_rows,
        as_of_ms=_f(candles[-1].get("close_time"))
        or _f(candles[-1].get("open_time")) + 3_600_000,
    )
    # Research observer is deliberately fail-closed if funding history is not
    # sufficient.  This is stricter than treating missing funding as acceptable.
    funding_allowed = funding_pct is not None and funding_pct < FUNDING_MAX_PCT

    return EdgeSnapshot(
        signal_open_time=_f(candles[-1].get("open_time")),
        close=close_now,
        ema=float(ema_now),
        previous_close=close_prev,
        previous_ema=float(ema_prev),
        crossed_above=crossed_above,
        crossed_below_exit=crossed_below_exit,
        funding_3d=funding_3d,
        funding_percentile=funding_pct,
        funding_allowed=funding_allowed,
    )


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "strategy_id": STRATEGY_ID,
        "promotion_eligible": False,
        "live_eligible": False,
        "position": None,
        "closed": 0,
        "wins": 0,
        "losses": 0,
        "gross_profit_usdt": 0.0,
        "gross_loss_abs_usdt": 0.0,
        "net_pnl_usdt": 0.0,
        "max_drawdown_usdt": 0.0,
        "equity_usdt": 0.0,
        "equity_peak_usdt": 0.0,
        "last_evaluated_open_time": 0.0,
        "last_event": None,
    }


def load_state(raw: str | None) -> dict[str, Any]:
    state = default_state()
    if not raw:
        return state
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return state
    if not isinstance(payload, dict):
        return state
    for key in state:
        if key in payload:
            state[key] = payload[key]
    if state.get("strategy_id") != STRATEGY_ID:
        return default_state()
    return state


def dump_state(state: dict[str, Any]) -> str:
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def _trade_pnl(
    *,
    entry_price: float,
    exit_price: float,
    quantity: float,
    fee_rate: float,
) -> float:
    gross = (exit_price - entry_price) * quantity
    entry_fee = entry_price * quantity * fee_rate
    exit_fee = exit_price * quantity * fee_rate
    return gross - entry_fee - exit_fee


def _stats_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    closed = int(state.get("closed", 0) or 0)
    wins = int(state.get("wins", 0) or 0)
    gross_profit = _f(state.get("gross_profit_usdt"))
    gross_loss_abs = _f(state.get("gross_loss_abs_usdt"))
    net = _f(state.get("net_pnl_usdt"))
    return {
        "closed": closed,
        "wins": wins,
        "losses": int(state.get("losses", 0) or 0),
        "win_rate": wins / closed if closed else None,
        "gross_profit_usdt": round(gross_profit, 8),
        "gross_loss_abs_usdt": round(gross_loss_abs, 8),
        "net_pnl_usdt": round(net, 8),
        "expectancy_usdt": round(net / closed, 8) if closed else None,
        "profit_factor": round(gross_profit / gross_loss_abs, 8)
        if gross_loss_abs > 0
        else None,
        "max_drawdown_usdt": round(_f(state.get("max_drawdown_usdt")), 8),
        "open": state.get("position") is not None,
    }


def process_cycle(
    *,
    state: dict[str, Any],
    candles: list[dict[str, Any]],
    funding_rows: Iterable[dict[str, Any]],
    quote_size_usdt: float,
    fee_rate: float = FEE_RATE,
    entry_ask: float | None = None,
    exit_bid: float | None = None,
    slippage_bps: float = SLIPPAGE_BPS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot = evaluate_snapshot(candles, funding_rows)
    current_open_time = snapshot.signal_open_time
    output: dict[str, Any] = {
        "strategy_id": STRATEGY_ID,
        "symbol": SYMBOL,
        "research_only": True,
        "promotion_eligible": False,
        "live_eligible": False,
        "snapshot": snapshot.to_dict(),
        "event": None,
    }

    if current_open_time <= _f(state.get("last_evaluated_open_time")):
        output["stats"] = _stats_snapshot(state)
        output["position"] = state.get("position")
        return state, output

    position = state.get("position")
    last_candle = candles[-1]
    close = snapshot.close
    high = _f(last_candle.get("high"), close)
    low = _f(last_candle.get("low"), close)

    if isinstance(position, dict):
        prior_high_water = max(_f(position.get("high_water")), _f(position.get("entry_price")))
        trailing_trigger = prior_high_water * (1.0 - TRAILING_STOP_PCT)
        exit_reason: str | None = None
        raw_exit: float | None = None

        # Use only the high-water known before this candle for the intrabar
        # trailing stop, avoiding optimistic assumptions about high/low order.
        if low <= trailing_trigger:
            exit_reason = "trailing_stop"
            raw_exit = trailing_trigger
        elif snapshot.crossed_below_exit:
            exit_reason = "ema_cross_down"
            raw_exit = exit_bid if exit_bid and exit_bid > 0 else close

        if exit_reason is not None and raw_exit is not None:
            slip = max(slippage_bps, 0.0) / 10_000.0
            executed_exit = raw_exit * (1.0 - slip)
            entry_price = _f(position.get("entry_price"))
            quantity = _f(position.get("quantity"))
            pnl = _trade_pnl(
                entry_price=entry_price,
                exit_price=executed_exit,
                quantity=quantity,
                fee_rate=fee_rate,
            )
            state["closed"] = int(state.get("closed", 0) or 0) + 1
            if pnl > 0:
                state["wins"] = int(state.get("wins", 0) or 0) + 1
                state["gross_profit_usdt"] = _f(state.get("gross_profit_usdt")) + pnl
            else:
                state["losses"] = int(state.get("losses", 0) or 0) + 1
                state["gross_loss_abs_usdt"] = _f(state.get("gross_loss_abs_usdt")) + abs(pnl)
            state["net_pnl_usdt"] = _f(state.get("net_pnl_usdt")) + pnl
            state["equity_usdt"] = _f(state.get("equity_usdt")) + pnl
            state["equity_peak_usdt"] = max(
                _f(state.get("equity_peak_usdt")),
                _f(state.get("equity_usdt")),
            )
            drawdown = _f(state.get("equity_peak_usdt")) - _f(state.get("equity_usdt"))
            state["max_drawdown_usdt"] = max(
                _f(state.get("max_drawdown_usdt")),
                drawdown,
            )
            event = {
                "type": "shadow_close",
                "reason": exit_reason,
                "entry_price": entry_price,
                "exit_price": executed_exit,
                "pnl_usdt_net_fees": round(pnl, 8),
                "opened_at": position.get("opened_at"),
                "closed_at": current_open_time,
            }
            state["position"] = None
            state["last_event"] = event
            output["event"] = event
        else:
            position["high_water"] = max(prior_high_water, high)
            position["last_processed_open_time"] = current_open_time
            state["position"] = position
    else:
        if snapshot.crossed_above and snapshot.funding_allowed:
            raw_entry = entry_ask if entry_ask and entry_ask > 0 else close
            slip = max(slippage_bps, 0.0) / 10_000.0
            executed_entry = raw_entry * (1.0 + slip)
            if quote_size_usdt <= 0 or executed_entry <= 0:
                raise ValueError("invalid_shadow_position_size")
            quantity = quote_size_usdt / executed_entry
            position = {
                "entry_price": executed_entry,
                "quantity": quantity,
                "quote_size_usdt": quote_size_usdt,
                "opened_at": current_open_time,
                "signal_open_time": current_open_time,
                "high_water": max(executed_entry, high),
                "last_processed_open_time": current_open_time,
                "funding_percentile_at_entry": snapshot.funding_percentile,
            }
            state["position"] = position
            event = {
                "type": "shadow_open",
                "entry_price": executed_entry,
                "quote_size_usdt": quote_size_usdt,
                "funding_percentile": snapshot.funding_percentile,
                "opened_at": current_open_time,
            }
            state["last_event"] = event
            output["event"] = event

    state["last_evaluated_open_time"] = current_open_time
    output["position"] = state.get("position")
    output["stats"] = _stats_snapshot(state)
    return state, output


def _fetch_funding_rows(timeout: float = 10.0) -> list[dict[str, Any]]:
    response = httpx.get(
        FUTURES_FUNDING_URL,
        params={"symbol": SYMBOL, "limit": 1000},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError("unexpected_funding_history_response")
    return [row for row in data if isinstance(row, dict)]


def run_external_challenger(engine: Any) -> dict[str, Any]:
    """Run this imported edge as an isolated research-only forward observer."""
    base = {
        "strategy_id": STRATEGY_ID,
        "symbol": SYMBOL,
        "research_only": True,
        "promotion_eligible": False,
        "live_eligible": False,
        "enabled": getattr(engine.settings, "mode", "") == "shadow",
    }
    if not base["enabled"]:
        return {**base, "status": "inactive_outside_shadow"}

    try:
        candles = engine.market.klines(SYMBOL, "1h", 1000)
        funding_rows = _fetch_funding_rows()
        books = engine.market.book_tickers()
        book = books.get(SYMBOL, {}) if isinstance(books, dict) else {}
        state = load_state(engine._get_runtime_meta(STATE_META_KEY))
        state, result = process_cycle(
            state=state,
            candles=candles,
            funding_rows=funding_rows,
            quote_size_usdt=float(engine.settings.trade_size_usdt),
            fee_rate=float(engine.settings.paper_fee_rate),
            entry_ask=_f(book.get("ask")) or None,
            exit_bid=_f(book.get("bid")) or None,
        )
        engine._set_runtime_meta(STATE_META_KEY, dump_state(state))

        event = result.get("event")
        if isinstance(event, dict) and event.get("type") == "shadow_open":
            engine.notifier.send(
                "V2 EXTERNAL EDGE SHADOW OPEN BTCUSDT\n"
                f"Strategy: {STRATEGY_ID}\n"
                f"Entry: {float(event['entry_price']):.2f}\n"
                f"Funding pct: {float(event['funding_percentile']):.1f}\n"
                "Research only — NOT promotion/live eligible."
            )
        elif isinstance(event, dict) and event.get("type") == "shadow_close":
            engine.notifier.send(
                "V2 EXTERNAL EDGE SHADOW CLOSE BTCUSDT\n"
                f"Strategy: {STRATEGY_ID}\n"
                f"Reason: {event['reason']}\n"
                f"PnL net fees/slippage: {float(event['pnl_usdt_net_fees']):+.4f} USDT\n"
                "Research only — Live remains OFF."
            )
        return {**base, "status": "ok", **result}
    except Exception as exc:
        # The challenger is research-only.  Its data-source failure must never
        # interrupt the existing V2 tournament or loosen any safety gate.
        return {
            **base,
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
