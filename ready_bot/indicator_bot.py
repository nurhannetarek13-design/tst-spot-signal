#!/usr/bin/env python3
"""Indicator-only Binance Spot PAPER runtime.

No named trading strategies. The engine evaluates independent indicator groups,
applies hard market/risk vetoes, sizes by stop risk, and paper-executes with
protective stop/target/trailing management. Live trading is intentionally absent.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("TST_INDICATOR_CONFIG", str(ROOT / "indicator_config.json")))
STATE_PATH = Path(os.getenv("TST_INDICATOR_STATE", str(ROOT / "indicator_state.json")))
CFG = json.loads(CONFIG_PATH.read_text())

STABLES = {"USDC","FDUSD","TUSD","USDP","DAI","BUSD","USD1","RLUSD","USDE","EUR","AEUR","TRY","BRL","GBP","AUD"}
LEVERAGED_SUFFIXES = ("UP","DOWN","BULL","BEAR")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def request_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tst-indicator-paper/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def market(route):
    last = None
    for base in ("https://data-api.binance.vision", "https://api.binance.com", "https://api1.binance.com"):
        try:
            return request_json(base + route)
        except Exception as exc:
            last = exc
    raise RuntimeError(f"Binance market data unavailable for {route}: {last}")


def load_state():
    if not STATE_PATH.exists():
        return {
            "mode": "PAPER_ONLY",
            "engine": CFG["engine"],
            "cash_usdt": float(CFG["risk"]["starting_cash_usdt"]),
            "positions": {},
            "closed_trades": [],
            "day": datetime.now(timezone.utc).date().isoformat(),
            "day_pnl": 0.0,
            "seen": {},
            "signals": [],
            "blocked": [],
            "last_run": None,
        }
    s = json.loads(STATE_PATH.read_text())
    if s.get("mode") != "PAPER_ONLY":
        raise RuntimeError("Refusing non-paper state")
    return s


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)


def reset_day(state):
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("day") != today:
        state["day"] = today
        state["day_pnl"] = 0.0


def universe():
    uc = CFG["universe"]
    quote = uc["quote_asset"]
    info = market("/api/v3/exchangeInfo")
    tick = {x["symbol"]: x for x in market("/api/v3/ticker/24hr")}
    rows = []
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING" or s.get("quoteAsset") != quote or not s.get("isSpotTradingAllowed", True):
            continue
        base = s.get("baseAsset", "")
        if not base or base in STABLES or base.endswith(LEVERAGED_SUFFIXES):
            continue
        t = tick.get(s.get("symbol"), {})
        qv = float(t.get("quoteVolume") or 0)
        px = float(t.get("lastPrice") or 0)
        if px <= 0 or qv < float(uc["min_quote_volume_24h"]):
            continue
        rows.append({"symbol": s["symbol"], "quote_volume_24h": qv, "price": px})
    rows.sort(key=lambda x: x["quote_volume_24h"], reverse=True)
    return rows[: int(uc["max_symbols"])]


def closed_bars(symbol, timeframe, limit=None):
    limit = int(limit or CFG["lookback"])
    rows = market("/api/v3/klines?" + urllib.parse.urlencode({
        "symbol": symbol, "interval": timeframe, "limit": limit
    }))
    now_ms = time.time() * 1000
    out = []
    for r in rows:
        if int(r[6]) >= now_ms:
            continue
        out.append({
            "t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
            "c": float(r[4]), "v": float(r[5]), "qv": float(r[7]), "tq": float(r[10])
        })
    return out


def book(symbol):
    x = market(f"/api/v3/ticker/bookTicker?symbol={symbol}")
    bid, ask = float(x["bidPrice"]), float(x["askPrice"])
    if not (bid > 0 and ask > 0 and ask >= bid):
        raise RuntimeError("INVALID_BOOK")
    return bid, ask


def symbol_filters(symbol):
    data = market(f"/api/v3/exchangeInfo?symbol={symbol}")
    entry = next((s for s in data.get("symbols", []) if s.get("symbol") == symbol), None)
    if not entry:
        raise RuntimeError("SYMBOL_FILTERS_MISSING")
    fs = {x["filterType"]: x for x in entry.get("filters", [])}
    lot = fs.get("LOT_SIZE")
    notion = fs.get("NOTIONAL") or fs.get("MIN_NOTIONAL")
    if not lot or not notion:
        raise RuntimeError("EXCHANGE_FILTERS_MISSING")
    return {
        "min_notional": float(notion["minNotional"]),
        "max_notional": float(notion.get("maxNotional", "Infinity")),
        "min_qty": float(lot["minQty"]),
        "max_qty": float(lot["maxQty"]),
        "step_size": float(lot["stepSize"]),
    }


def ema(values, n):
    if len(values) < n:
        return None
    a = 2 / (n + 1)
    value = sum(values[:n]) / n
    for x in values[n:]:
        value = x * a + value * (1 - a)
    return value


def ema_series(values, n):
    if len(values) < n:
        return []
    out = [None] * (n - 1)
    value = sum(values[:n]) / n
    out.append(value)
    a = 2 / (n + 1)
    for x in values[n:]:
        value = x * a + value * (1 - a)
        out.append(value)
    return out


def rsi(values, n=14):
    if len(values) < n + 1:
        return None
    changes = [values[i] - values[i - 1] for i in range(len(values) - n, len(values))]
    gains = sum(max(x, 0) for x in changes) / n
    losses = sum(max(-x, 0) for x in changes) / n
    if losses <= 1e-15:
        return 100.0 if gains else 50.0
    return 100 - 100 / (1 + gains / losses)


def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    vals = []
    for i in range(len(bars) - n, len(bars)):
        prev = bars[i - 1]["c"]
        vals.append(max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - prev), abs(bars[i]["l"] - prev)))
    return sum(vals) / len(vals)


def macd_hist(values, fast=12, slow=26, signal=9):
    ef = ema_series(values, fast)
    es = ema_series(values, slow)
    if not ef or not es:
        return []
    m = []
    start = max(fast, slow) - 1
    for i in range(start, len(values)):
        if ef[i] is None or es[i] is None:
            continue
        m.append(ef[i] - es[i])
    sig = ema_series(m, signal)
    out = []
    for i in range(len(m)):
        out.append(None if i >= len(sig) or sig[i] is None else m[i] - sig[i])
    return out


def relative_quote_volume(bars, n=20):
    if len(bars) < n + 1:
        return None
    prior = [x["qv"] for x in bars[-n - 1:-1]]
    avg = sum(prior) / len(prior)
    return bars[-1]["qv"] / avg if avg > 0 else None


def taker_ratio(bar):
    return bar["tq"] / bar["qv"] if bar["qv"] > 0 else None


def spread_bps(bid, ask):
    mid = (bid + ask) / 2
    return ((ask - bid) / mid) * 10000 if mid > 0 else float("inf")


def bollinger_width(values, n=20):
    if len(values) < n:
        return None
    x = values[-n:]
    mean = sum(x) / n
    var = sum((v - mean) ** 2 for v in x) / n
    sd = math.sqrt(var)
    return (4 * sd / mean) if mean > 0 else None


def btc_regime(btc1h, btc4h):
    if min(len(btc1h), len(btc4h)) < 220:
        return {"ok": False, "reason": "BTC_HISTORY"}
    c1 = [x["c"] for x in btc1h]
    c4 = [x["c"] for x in btc4h]
    e20 = ema(c1, 20)
    e50 = ema(c1, 50)
    e50_4 = ema(c4, 50)
    e200_4 = ema(c4, 200)
    ret1 = c1[-1] / c1[-2] - 1
    ok = (
        e20 is not None and e50 is not None and e50_4 is not None and e200_4 is not None
        and e20 > e50 and e50_4 > e200_4
        and ret1 > float(CFG["entry"]["btc_max_1h_drop"])
    )
    return {"ok": bool(ok), "ret1h": ret1, "ema20_1h": e20, "ema50_1h": e50,
            "ema50_4h": e50_4, "ema200_4h": e200_4}


def indicator_snapshot(symbol, bars15, bars1h, bars4h, bid, ask, quote_volume_24h):
    if min(len(bars15), len(bars1h), len(bars4h)) < 220:
        raise RuntimeError("INSUFFICIENT_HISTORY")
    c15 = [x["c"] for x in bars15]
    c1 = [x["c"] for x in bars1h]
    c4 = [x["c"] for x in bars4h]
    last15 = bars15[-1]

    e20_1 = ema(c1, 20)
    e50_1 = ema(c1, 50)
    e200_1 = ema(c1, 200)
    e50_4 = ema(c4, 50)
    e200_4 = ema(c4, 200)

    r_now = rsi(c1)
    r_prev = rsi(c1[:-3]) if len(c1) > 20 else None
    mh = macd_hist(c1)
    mh_now = mh[-1] if mh else None
    mh_prev = mh[-2] if len(mh) > 1 else None

    tr = taker_ratio(last15)
    tr3_vals = [taker_ratio(x) for x in bars15[-3:]]
    tr3 = sum(x for x in tr3_vals if x is not None) / len([x for x in tr3_vals if x is not None]) if all(x is not None for x in tr3_vals) else None
    rv = relative_quote_volume(bars15, 20)

    a1 = atr(bars1h, 14)
    atr_pct = a1 / c1[-1] if a1 and c1[-1] > 0 else None
    bw = bollinger_width(c1, 20)
    spr = spread_bps(bid, ask)

    groups = {"trend": 0, "momentum": 0, "flow": 0, "volatility": 0, "liquidity": 0}

    if e50_4 and e200_4 and e50_4 > e200_4: groups["trend"] += 8
    if e20_1 and e50_1 and e20_1 > e50_1: groups["trend"] += 6
    if e50_1 and e200_1 and e50_1 > e200_1: groups["trend"] += 6
    if e20_1 and c1[-1] > e20_1: groups["trend"] += 5

    ec = CFG["entry"]
    if r_now is not None and float(ec["rsi_min"]) <= r_now <= float(ec["rsi_max"]): groups["momentum"] += 8
    if r_now is not None and r_prev is not None and r_now > r_prev: groups["momentum"] += 4
    if mh_now is not None and mh_now > 0: groups["momentum"] += 4
    if mh_now is not None and mh_prev is not None and mh_now > mh_prev: groups["momentum"] += 4

    if tr is not None and tr >= float(ec["min_taker_buy_ratio"]): groups["flow"] += 15
    if tr3 is not None and tr3 >= 0.54: groups["flow"] += 5
    if rv is not None and rv >= float(ec["min_relative_quote_volume"]): groups["flow"] += 10

    if atr_pct is not None and float(ec["atr_pct_min"]) <= atr_pct <= float(ec["atr_pct_max"]): groups["volatility"] += 6
    if bw is not None and 0.01 <= bw <= 0.12: groups["volatility"] += 4

    if quote_volume_24h >= float(CFG["universe"]["min_quote_volume_24h"]): groups["liquidity"] += 10
    if spr <= float(ec["max_spread_bps"]): groups["liquidity"] += 5

    score = sum(groups.values())
    vetoes = []
    if tr is None or tr < float(ec["hard_taker_floor"]): vetoes.append("TAKER_FLOW")
    if rv is None or rv < float(ec["hard_relative_volume_floor"]): vetoes.append("RELATIVE_VOLUME")
    if r_now is None or r_now > float(ec["rsi_veto"]): vetoes.append("RSI")
    if atr_pct is None or atr_pct > float(ec["atr_pct_veto"]): vetoes.append("VOLATILITY")
    if spr > float(ec["max_spread_bps"]): vetoes.append("SPREAD")
    if quote_volume_24h < float(CFG["universe"]["min_quote_volume_24h"]): vetoes.append("LIQUIDITY")

    return {
        "symbol": symbol,
        "score": score,
        "groups": groups,
        "vetoes": vetoes,
        "eligible": score >= float(ec["min_score"]) and not vetoes,
        "bar_time": last15["t"],
        "bid": bid, "ask": ask, "spread_bps": spr,
        "taker_buy_ratio": tr, "taker_buy_ratio_3": tr3, "relative_quote_volume": rv,
        "rsi_1h": r_now, "macd_hist_1h": mh_now, "atr_pct_1h": atr_pct,
        "bollinger_width_1h": bw,
        "quote_volume_24h": quote_volume_24h,
        "atr_1h": a1,
    }


def simulated_fill(price, side):
    slip = float(CFG["risk"]["slippage_rate"])
    return price * (1 + slip if side == "buy" else 1 - slip)


def stop_risk(position):
    fee = float(CFG["risk"]["fee_rate"])
    stop_fill = simulated_fill(position["stop"], "sell")
    return max(0.0, position["cost"] - position["qty"] * stop_fill * (1 - fee))


def portfolio_stop_risk(state):
    return sum(stop_risk(p) for p in state["positions"].values())


def close_position(state, symbol, bid, reason):
    p = state["positions"].pop(symbol)
    fee = float(CFG["risk"]["fee_rate"])
    ref = min(bid, p["stop"]) if reason == "STOP" else (p["target"] if reason == "TARGET" else bid)
    fill = simulated_fill(ref, "sell")
    proceeds = p["qty"] * fill * (1 - fee)
    pnl = proceeds - p["cost"]
    state["cash_usdt"] += proceeds
    state["day_pnl"] += pnl
    state["closed_trades"].append({
        "symbol": symbol, "engine": CFG["engine"], "entry": p["entry"], "exit": fill,
        "qty": p["qty"], "pnl_usdt": pnl, "reason": reason,
        "score_at_entry": p.get("score"), "opened_at": p["opened_at"], "closed_at": now_iso(),
    })


def round_qty(qty, filters):
    step = float(filters["step_size"])
    if not math.isfinite(step) or step <= 0:
        return 0.0
    return math.floor(qty / step + 1e-12) * step


def open_position(state, snap, filters):
    risk_cfg = CFG["risk"]
    if len(state["positions"]) >= int(risk_cfg["max_open_positions"]):
        return "MAX_OPEN_POSITIONS"
    if state["day_pnl"] <= -abs(float(risk_cfg["max_daily_loss_usdt"])):
        return "DAILY_LOSS_CAP"
    if snap["symbol"] in state["positions"]:
        return "POSITION_ALREADY_OPEN"

    entry = simulated_fill(snap["ask"], "buy")
    atr_pct = float(snap["atr_pct_1h"])
    stop_fraction = max(float(risk_cfg["min_stop_fraction"]),
                        min(float(risk_cfg["max_stop_fraction"]),
                            atr_pct * float(risk_cfg["stop_atr_multiplier"])))
    stop = entry * (1 - stop_fraction)
    target = entry * (1 + stop_fraction * float(risk_cfg["reward_risk"]))

    fee = float(risk_cfg["fee_rate"])
    risk_budget = float(risk_cfg["max_risk_per_trade_usdt"])
    stake_by_risk = risk_budget / max(stop_fraction + 2 * fee + float(risk_cfg["slippage_rate"]), 1e-9)
    notional = min(float(risk_cfg["max_quote_per_trade_usdt"]), stake_by_risk,
                   state["cash_usdt"] / (1 + fee))
    if notional <= 0:
        return "NO_CASH"
    if not (float(filters["min_notional"]) <= notional <= float(filters["max_notional"])):
        return "EXCHANGE_NOTIONAL_FILTER"

    qty = round_qty(notional / entry, filters)
    if qty < float(filters["min_qty"]) or qty > float(filters["max_qty"]):
        return "EXCHANGE_LOT_FILTER"
    cost = qty * entry * (1 + fee)
    if cost > state["cash_usdt"] + 1e-9:
        return "INSUFFICIENT_CASH"

    pos = {
        "engine": CFG["engine"], "entry": entry, "qty": qty, "cost": cost,
        "stop": stop, "target": target, "initial_stop": stop,
        "initial_risk_abs": entry - stop, "breakeven": False,
        "opened_at": now_iso(), "bar_time": snap["bar_time"], "score": snap["score"],
        "groups": snap["groups"],
    }
    actual = stop_risk(pos)
    if actual > float(risk_cfg["max_risk_per_trade_usdt"]) + 1e-9:
        return "RISK_PER_TRADE"
    portfolio = portfolio_stop_risk(state) + actual
    if portfolio > float(risk_cfg["max_portfolio_stop_risk_usdt"]) + 1e-9:
        return "PORTFOLIO_STOP_RISK"
    if max(0.0, -state["day_pnl"]) + portfolio > float(risk_cfg["max_daily_loss_usdt"]) + 1e-9:
        return "REMAINING_DAILY_RISK"

    state["cash_usdt"] -= cost
    state["positions"][snap["symbol"]] = pos
    return "PAPER_OPENED"


def manage_position(state, symbol, bid, atr_now):
    p = state["positions"][symbol]
    if bid <= p["stop"]:
        close_position(state, symbol, bid, "STOP")
        return
    if bid >= p["target"]:
        close_position(state, symbol, bid, "TARGET")
        return
    if p["initial_risk_abs"] > 0 and bid >= p["entry"] + float(CFG["risk"]["breakeven_at_r"]) * p["initial_risk_abs"]:
        p["stop"] = max(p["stop"], p["entry"])
        p["breakeven"] = True
    if p["breakeven"] and atr_now and atr_now > 0:
        trail = bid - float(CFG["risk"]["trailing_atr_multiplier"]) * atr_now
        p["stop"] = max(p["stop"], trail)


def validate_config():
    if CFG.get("mode") != "paper":
        raise RuntimeError("Indicator runtime is PAPER only")
    if CFG.get("engine") != "INDICATOR_ONLY_V1":
        raise RuntimeError("Unexpected engine id")
    if float(CFG["entry"]["min_score"]) > 100 or float(CFG["entry"]["min_score"]) <= 0:
        raise RuntimeError("Invalid score threshold")
    if float(CFG["risk"]["max_daily_loss_usdt"]) <= 0:
        raise RuntimeError("Invalid daily loss cap")
    if float(CFG["risk"]["max_risk_per_trade_usdt"]) <= 0:
        raise RuntimeError("Invalid per-trade risk")


def main():
    validate_config()
    state = load_state()
    reset_day(state)
    blocked = []

    try:
        uni = universe()
    except Exception as exc:
        state["blocked"] = [{"reason": "UNIVERSE_UNAVAILABLE", "detail": str(exc)[:160]}]
        state["last_run"] = now_iso()
        save_state(state)
        print(json.dumps(state, indent=2))
        return state

    try:
        btc1h = closed_bars("BTCUSDT", CFG["timeframes"]["trend"])
        btc4h = closed_bars("BTCUSDT", CFG["timeframes"]["macro"])
        btc = btc_regime(btc1h, btc4h)
    except Exception as exc:
        btc = {"ok": False, "reason": f"BTC_DATA:{str(exc)[:120]}"}

    snapshots = {}
    qv_map = {x["symbol"]: x["quote_volume_24h"] for x in uni}
    # Always retain pricing for open positions even if they fall out of the dynamic universe.
    symbols = list(dict.fromkeys([x["symbol"] for x in uni] + list(state["positions"])))
    for symbol in symbols:
        try:
            b15 = closed_bars(symbol, CFG["timeframes"]["trigger"])
            b1 = closed_bars(symbol, CFG["timeframes"]["trend"])
            b4 = closed_bars(symbol, CFG["timeframes"]["macro"])
            bid, ask = book(symbol)
            qv = qv_map.get(symbol)
            if qv is None:
                t = market(f"/api/v3/ticker/24hr?symbol={symbol}")
                qv = float(t.get("quoteVolume") or 0)
            snap = indicator_snapshot(symbol, b15, b1, b4, bid, ask, qv)
            snap["filters"] = symbol_filters(symbol)
            snapshots[symbol] = snap
        except Exception as exc:
            blocked.append({"symbol": symbol, "reason": "DATA_UNAVAILABLE", "detail": str(exc)[:160]})

    unpriced = False
    for symbol in list(state["positions"]):
        snap = snapshots.get(symbol)
        if not snap:
            blocked.append({"symbol": symbol, "reason": "OPEN_POSITION_UNPRICED"})
            unpriced = True
            continue
        manage_position(state, symbol, snap["bid"], snap["atr_1h"])

    signals = []
    if not btc.get("ok"):
        blocked.append({"reason": "BTC_REGIME_VETO", "detail": btc})
    else:
        for symbol in [x["symbol"] for x in uni]:
            if symbol in state["positions"]:
                continue
            snap = snapshots.get(symbol)
            if not snap:
                continue
            if snap["eligible"]:
                signals.append(snap)

    signals.sort(key=lambda x: (-x["score"], -x["taker_buy_ratio"], -x["relative_quote_volume"], x["symbol"]))

    if unpriced:
        blocked.append({"reason": "GLOBAL_ENTRY_HALT_UNPRICED_POSITION"})
    elif state["day_pnl"] <= -abs(float(CFG["risk"]["max_daily_loss_usdt"])):
        blocked.append({"reason": "DAILY_LOSS_CAP"})
    else:
        for snap in signals:
            if len(state["positions"]) >= int(CFG["risk"]["max_open_positions"]):
                break
            key = snap["symbol"]
            if state.get("seen", {}).get(key) == snap["bar_time"]:
                continue
            result = open_position(state, snap, snap["filters"])
            state.setdefault("seen", {})[key] = snap["bar_time"]
            if result != "PAPER_OPENED":
                blocked.append({"symbol": key, "score": snap["score"], "reason": result})

    state["signals"] = [{
        "symbol": x["symbol"], "score": x["score"], "groups": x["groups"],
        "taker_buy_ratio": x["taker_buy_ratio"], "relative_quote_volume": x["relative_quote_volume"],
        "spread_bps": x["spread_bps"], "bar_time": x["bar_time"],
    } for x in signals]
    state["blocked"] = blocked
    state["btc_regime"] = btc
    state["universe"] = [x["symbol"] for x in uni]
    state["last_run"] = now_iso()
    save_state(state)

    print(json.dumps({
        "mode": "PAPER_ONLY", "engine": CFG["engine"], "cash_usdt": state["cash_usdt"],
        "day_pnl": state["day_pnl"], "btc_regime": btc, "positions": state["positions"],
        "signals": state["signals"], "blocked": blocked, "last_run": state["last_run"],
    }, indent=2))
    return state


if __name__ == "__main__":
    main()
