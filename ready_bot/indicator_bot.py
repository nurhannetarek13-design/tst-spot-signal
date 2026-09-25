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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    if not str(route).startswith("/api/v3/"):
        raise RuntimeError("SPOT_ONLY_ROUTE_VIOLATION")
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
    s["engine"] = CFG["engine"]
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

def precision_evidence_status(config=None):
    config = config or CFG
    gate = config.get("evidence_gate", {})
    if not gate.get("required", False):
        return {"ok": True, "reason": "NOT_REQUIRED"}
    path = ROOT.parent / str(gate.get("report_path", ""))
    try:
        report = json.loads(path.read_text())
    except Exception as exc:
        return {"ok": False, "reason": "EVIDENCE_REPORT_UNAVAILABLE", "detail": str(exc)[:120]}
    holdout = report.get("holdout") or {}
    trades = int(holdout.get("trades") or 0)
    win_rate = float(holdout.get("winRate") or 0.0)
    net = float(holdout.get("netPnlPerUnit") or 0.0)
    claim = bool(report.get("claimSupported"))
    required_rate = float(gate.get("minimum_win_rate", 0.99))
    required_trades = int(gate.get("minimum_holdout_trades", 100))
    positive_net_ok = (net > 0) if gate.get("require_positive_net", True) else True
    ok = bool(claim and trades >= required_trades and win_rate >= required_rate and positive_net_ok)
    return {
        "ok": ok,
        "reason": "EVIDENCE_CONFIRMED" if ok else "PRECISION_EVIDENCE_NOT_MET",
        "claimSupported": claim,
        "holdoutTrades": trades,
        "holdoutWinRate": win_rate,
        "holdoutNetPnlPerUnit": net,
        "requiredWinRate": required_rate,
        "requiredTrades": required_trades,
    }


def evidence_blocks_entries(evidence, config=None):
    config = config or CFG
    gate = config.get("evidence_gate", {})
    scope = gate.get("scope", "all_entries")
    if scope == "live_only" and config.get("mode") == "paper":
        return False
    return not evidence.get("ok", False)


def is_spot_symbol_record(record, quote_asset=None):
    quote_asset = quote_asset or CFG["universe"]["quote_asset"]
    if record.get("status") != "TRADING":
        return False
    if record.get("quoteAsset") != quote_asset:
        return False
    if record.get("isSpotTradingAllowed") is not True:
        return False
    # Binance Spot exchangeInfo can return legacy permissions=[] while
    # permissionSets carries SPOT. isSpotTradingAllowed=true is authoritative;
    # when permissionSets are present, require SPOT there as a consistency check.
    permission_sets = record.get("permissionSets")
    if permission_sets:
        if not any("SPOT" in permission_set for permission_set in permission_sets):
            return False
    base = record.get("baseAsset", "")
    if not base or base in STABLES or base.endswith(LEVERAGED_SUFFIXES):
        return False
    return True


def universe():
    uc = CFG["universe"]
    quote = uc["quote_asset"]
    info = market("/api/v3/exchangeInfo")
    tick = {x["symbol"]: x for x in market("/api/v3/ticker/24hr")}
    rows = []
    for s in info.get("symbols", []):
        if not is_spot_symbol_record(s, quote):
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
    if not is_spot_symbol_record(entry):
        raise RuntimeError("SPOT_ONLY_SYMBOL_VIOLATION")
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
        "spot_verified": True,
        "quote_asset": entry.get("quoteAsset"),
        "symbol": entry.get("symbol"),
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



def adx(bars, n=14):
    """Latest Wilder ADX from OHLC bars."""
    if len(bars) < (2 * n + 2):
        return None
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(bars)):
        cur, prev = bars[i], bars[i - 1]
        up = cur["h"] - prev["h"]
        down = prev["l"] - cur["l"]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    if len(trs) < n * 2:
        return None
    tr_s = sum(trs[:n])
    plus_s = sum(plus_dm[:n])
    minus_s = sum(minus_dm[:n])
    dx = []
    for i in range(n, len(trs)):
        if i > n:
            tr_s = tr_s - tr_s / n + trs[i]
            plus_s = plus_s - plus_s / n + plus_dm[i]
            minus_s = minus_s - minus_s / n + minus_dm[i]
        if tr_s <= 0:
            continue
        pdi = 100.0 * plus_s / tr_s
        mdi = 100.0 * minus_s / tr_s
        den = pdi + mdi
        dx.append(0.0 if den <= 0 else 100.0 * abs(pdi - mdi) / den)
    if len(dx) < n:
        return None
    value = sum(dx[:n]) / n
    for x in dx[n:]:
        value = (value * (n - 1) + x) / n
    return value


def session_vwap_series(bars):
    """UTC-session VWAP for each bar using typical price * base volume."""
    out = []
    current_day = None
    pv = 0.0
    vol = 0.0
    for b in bars:
        day = datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc).date()
        if day != current_day:
            current_day = day
            pv = 0.0
            vol = 0.0
        typical = (b["h"] + b["l"] + b["c"]) / 3.0
        pv += typical * b["v"]
        vol += b["v"]
        out.append(pv / vol if vol > 0 else None)
    return out


def successful_vwap_retest(bars, vwaps, lookback=4, tolerance_pct=0.0025):
    if not bars or not vwaps or vwaps[-1] is None or bars[-1]["c"] <= vwaps[-1]:
        return False
    start = max(0, len(bars) - int(lookback))
    for i in range(start, len(bars)):
        v = vwaps[i]
        if v is None:
            continue
        if bars[i]["l"] <= v * (1.0 + float(tolerance_pct)) and bars[i]["c"] >= v:
            return True
    return False


def confirmed_swing_low(bars, lookback=20, left=2, right=2):
    """Most recent confirmed pivot low; excludes the unconfirmed right edge."""
    if len(bars) < left + right + 3:
        return None
    lo = max(int(left), len(bars) - int(lookback))
    hi = len(bars) - int(right)
    for i in range(hi - 1, lo - 1, -1):
        pivot = bars[i]["l"]
        left_vals = [bars[j]["l"] for j in range(i - int(left), i)]
        right_vals = [bars[j]["l"] for j in range(i + 1, i + int(right) + 1)]
        if pivot < min(left_vals) and pivot <= min(right_vals):
            return float(pivot)
    return None


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
    one_hour_bullish = bool(e20 is not None and e50 is not None and e20 > e50)
    macro_bullish = bool(e50_4 is not None and e200_4 is not None and e50_4 > e200_4)
    crash_veto_clear = bool(ret1 > float(CFG["entry"]["btc_max_1h_drop"]))
    # For PAPER observation, 1h softness is no longer a hard veto. We still
    # require a bullish 4h regime and no sharp BTC drop.
    ok = bool(macro_bullish and crash_veto_clear)
    return {"ok": ok, "ret1h": ret1, "oneHourBullish": one_hour_bullish,
            "macroBullish": macro_bullish, "crashVetoClear": crash_veto_clear,
            "ema20_1h": e20, "ema50_1h": e50,
            "ema50_4h": e50_4, "ema200_4h": e200_4}


def indicator_snapshot(symbol, bars15, bars1h, bars4h, bid, ask, quote_volume_24h):
    if min(len(bars15), len(bars1h), len(bars4h)) < 220:
        raise RuntimeError("INSUFFICIENT_HISTORY")

    c15 = [x["c"] for x in bars15]
    c1 = [x["c"] for x in bars1h]
    last15 = bars15[-1]
    ec = CFG["entry"]

    # 6 entry indicators only.
    ema20_15 = ema(c15, 20)
    ema50_15 = ema(c15, 50)
    ema200_1h = ema(c1, 200)
    adx15 = adx(bars15, 14)
    rsi15 = rsi(c15, 14)
    rvol20 = relative_quote_volume(bars15, 20)
    taker = taker_ratio(last15)
    vwaps = session_vwap_series(bars15)
    vwap_now = vwaps[-1] if vwaps else None
    vwap_retest = successful_vwap_retest(
        bars15,
        vwaps,
        lookback=int(ec["vwap_retest_lookback"]),
        tolerance_pct=float(ec["vwap_retest_tolerance_pct"]),
    )

    atr15 = atr(bars15, 14)
    atr_pct15 = atr15 / c15[-1] if atr15 and c15[-1] > 0 else None
    swing = confirmed_swing_low(
        bars15,
        lookback=int(CFG["risk"]["swing_lookback_bars"]),
        left=int(CFG["risk"]["swing_pivot_left"]),
        right=int(CFG["risk"]["swing_pivot_right"]),
    )
    spr = spread_bps(bid, ask)

    checks = {
        "ema_20_gt_50_15m": bool(ema20_15 is not None and ema50_15 is not None and ema20_15 > ema50_15),
        "adx_14": bool(adx15 is not None and adx15 >= float(ec["adx_min"])),
        "rsi_14": bool(rsi15 is not None and float(ec["rsi_min"]) <= rsi15 <= float(ec["rsi_max"])),
        "rvol_20": bool(rvol20 is not None and rvol20 >= float(ec["min_relative_quote_volume"])),
        "taker_buy_ratio": bool(taker is not None and taker >= float(ec["min_taker_buy_ratio"])),
        "vwap_retest": bool(vwap_retest),
    }
    score = sum(1 for passed in checks.values() if passed)

    # Hard market gates are separate from the 5/6 score.
    vetoes = []
    if ema200_1h is None or c1[-1] <= ema200_1h:
        vetoes.append("EMA200_1H")
    if quote_volume_24h < float(CFG["universe"]["min_quote_volume_24h"]):
        vetoes.append("LIQUIDITY")
    if spr > float(ec["max_spread_bps"]):
        vetoes.append("SPREAD")
    if taker is None or taker < float(ec["hard_taker_floor"]):
        vetoes.append("TAKER_FLOW_LT_50")
    if rsi15 is None or rsi15 > float(ec["rsi_veto"]):
        vetoes.append("RSI_LATE")

    return {
        "symbol": symbol,
        "score": score,
        "score_total": int(ec["score_total"]),
        "checks": checks,
        "vetoes": vetoes,
        "eligible": score >= int(ec["score_required"]) and not vetoes,
        "bar_time": last15["t"],
        "bid": bid,
        "ask": ask,
        "spread_bps": spr,
        "ema20_15m": ema20_15,
        "ema50_15m": ema50_15,
        "ema200_1h": ema200_1h,
        "adx_15m": adx15,
        "rsi_15m": rsi15,
        "relative_quote_volume": rvol20,
        "taker_buy_ratio": taker,
        "vwap_15m": vwap_now,
        "vwap_retest": vwap_retest,
        "atr_15m": atr15,
        "atr_pct_15m": atr_pct15,
        "confirmed_swing_low": swing,
        "quote_volume_24h": quote_volume_24h,
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
    if snap.get("eligible") is not True or snap.get("vetoes"):
        return "SIGNAL_NOT_ELIGIBLE"
    if filters.get("spot_verified") is not True or filters.get("quote_asset") != CFG["universe"]["quote_asset"]:
        return "SPOT_ONLY_VIOLATION"
    if filters.get("symbol") != snap.get("symbol"):
        return "SYMBOL_FILTER_MISMATCH"
    if not math.isfinite(float(snap.get("ask", 0))) or float(snap.get("ask", 0)) <= 0:
        return "INVALID_ENTRY_PRICE"
    risk_cfg = CFG["risk"]
    if len(state["positions"]) >= int(risk_cfg["max_open_positions"]):
        return "MAX_OPEN_POSITIONS"
    if state["day_pnl"] <= -abs(float(risk_cfg["max_daily_loss_usdt"])):
        return "DAILY_LOSS_CAP"
    if snap["symbol"] in state["positions"]:
        return "POSITION_ALREADY_OPEN"

    entry = simulated_fill(snap["ask"], "buy")
    atr_abs = snap.get("atr_15m")
    swing_low = snap.get("confirmed_swing_low")
    if atr_abs is None or not math.isfinite(float(atr_abs)) or float(atr_abs) <= 0:
        return "ATR_UNAVAILABLE"
    if swing_low is None or not math.isfinite(float(swing_low)) or float(swing_low) <= 0:
        return "NO_CONFIRMED_SWING_LOW"

    atr_abs = float(atr_abs)
    swing_low = float(swing_low)
    atr_stop = entry - atr_abs * float(risk_cfg["stop_atr_multiplier"])
    swing_stop = swing_low - atr_abs * float(risk_cfg["swing_atr_buffer"])
    stop = min(atr_stop, swing_stop, entry * (1 - float(risk_cfg["min_stop_fraction"])))
    if stop <= 0 or stop >= entry:
        return "INVALID_STOP"
    stop_fraction = (entry - stop) / entry
    if stop_fraction > float(risk_cfg["max_stop_fraction"]):
        return "STOP_TOO_WIDE"
    target = entry + (entry - stop) * float(risk_cfg["reward_risk"])

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
        "score_total": snap.get("score_total", 6), "checks": snap.get("checks", {}),
        "confirmed_swing_low": swing_low,
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


def fetch_symbol_snapshot(symbol, quote_volume_hint=None):
    b15 = closed_bars(symbol, CFG["timeframes"]["trigger"])
    b1 = closed_bars(symbol, CFG["timeframes"]["trend"])
    b4 = closed_bars(symbol, CFG["timeframes"]["macro"])
    bid, ask = book(symbol)
    qv = quote_volume_hint
    if qv is None:
        t = market(f"/api/v3/ticker/24hr?symbol={symbol}")
        qv = float(t.get("quoteVolume") or 0)
    snap = indicator_snapshot(symbol, b15, b1, b4, bid, ask, qv)
    snap["filters"] = symbol_filters(symbol)
    return snap


def validate_config():
    if CFG.get("market_type") != "spot":
        raise RuntimeError("SPOT_ONLY_CONFIG_VIOLATION")
    if CFG.get("mode") != "paper":
        raise RuntimeError("Indicator runtime is PAPER only")
    if CFG.get("engine") != "INDICATOR_ONLY_V2_5OF6":
        raise RuntimeError("Unexpected engine id")
    if int(CFG["entry"]["score_total"]) != 6 or int(CFG["entry"]["score_required"]) != 5:
        raise RuntimeError("Invalid 5-of-6 score contract")
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
    # Market reads are independent. Parallelizing them keeps a 5-minute runtime
    # cadence practical without changing any indicator or risk decision.
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(symbols)))) as pool:
        futures = {pool.submit(fetch_symbol_snapshot, symbol, qv_map.get(symbol)): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                snapshots[symbol] = future.result()
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

    evidence = precision_evidence_status()
    signals = []
    if evidence_blocks_entries(evidence):
        blocked.append({"reason": "PRECISION_EVIDENCE_GATE", "detail": evidence})
    elif not btc.get("ok"):
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
        "symbol": x["symbol"], "score": x["score"], "score_total": x["score_total"], "checks": x["checks"],
        "taker_buy_ratio": x["taker_buy_ratio"], "relative_quote_volume": x["relative_quote_volume"],
        "adx_15m": x["adx_15m"], "rsi_15m": x["rsi_15m"], "vwap_retest": x["vwap_retest"],
        "spread_bps": x["spread_bps"], "bar_time": x["bar_time"],
    } for x in signals]
    state["blocked"] = blocked
    state["btc_regime"] = btc
    state["precision_evidence"] = evidence
    state["universe"] = [x["symbol"] for x in uni]
    state["last_run"] = now_iso()
    save_state(state)

    print(json.dumps({
        "mode": "PAPER_ONLY", "engine": CFG["engine"], "cash_usdt": state["cash_usdt"],
        "day_pnl": state["day_pnl"], "btc_regime": btc, "precision_evidence": evidence, "positions": state["positions"],
        "signals": state["signals"], "blocked": blocked, "last_run": state["last_run"],
    }, indent=2))
    return state


if __name__ == "__main__":
    main()
