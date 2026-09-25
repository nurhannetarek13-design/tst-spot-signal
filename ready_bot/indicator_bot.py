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

try:
    from .early_momentum import (
        aggtrade_delta,
        combine_microstructure,
        depth_imbalance,
        micro_breakout_hold,
        prefilter_snapshot,
    )
except ImportError:
    from early_momentum import (
        aggtrade_delta,
        combine_microstructure,
        depth_imbalance,
        micro_breakout_hold,
        prefilter_snapshot,
    )

try:
    from .market_context import (
        classify_market_regime,
        max_open_position_correlation,
        pct_return,
        relative_strength_ranking,
        validate_bar_integrity,
    )
except ImportError:
    from market_context import (
        classify_market_regime,
        max_open_position_correlation,
        pct_return,
        relative_strength_ranking,
        validate_bar_integrity,
    )

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
            "c": float(r[4]), "v": float(r[5]), "qv": float(r[7]), "n": int(r[8]), "tq": float(r[10])
        })
    integrity = validate_bar_integrity(out, timeframe, int(now_ms))
    if not integrity["ok"]:
        raise RuntimeError("DATA_INTEGRITY:" + ",".join(integrity["reasons"]))
    return out


def book(symbol):
    x = market(f"/api/v3/ticker/bookTicker?symbol={symbol}")
    bid, ask = float(x["bidPrice"]), float(x["askPrice"])
    if not (bid > 0 and ask > 0 and ask >= bid):
        raise RuntimeError("INVALID_BOOK")
    return bid, ask


def depth5(symbol):
    return market("/api/v3/depth?" + urllib.parse.urlencode({"symbol": symbol, "limit": 5}))


def recent_aggtrades(symbol, limit=500):
    return market("/api/v3/aggTrades?" + urllib.parse.urlencode({"symbol": symbol, "limit": int(limit)}))


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
        "context_pass_5of6": score >= int(ec["score_required"]),
        "eligible": False,
        "guard_ok": not vetoes,
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


def account_equity_at_cost(state):
    return float(state.get("cash_usdt", 0.0)) + sum(float(p.get("cost", 0.0)) for p in state.get("positions", {}).values())


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
        "momentum_score": p.get("momentum_score"),
        "market_regime": p.get("market_regime"),
        "relative_strength": p.get("relative_strength"),
        "entry_context": p.get("entry_context"),
        "mfe_r": p.get("mfe_r"),
        "mae_r": p.get("mae_r"),
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
    equity = account_equity_at_cost(state)
    regime_mult=max(0.0,min(1.0,float(snap.get("risk_multiplier",1.0))))
    risk_budget = min(
        float(risk_cfg["max_risk_per_trade_usdt"]),
        equity * float(risk_cfg["max_risk_per_trade_fraction"]),
    ) * regime_mult
    if risk_budget <= 0:
        return "REGIME_RISK_ZERO"
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
        "momentum_score": snap.get("micro", {}).get("score"),
        "momentum_stage": snap.get("micro", {}).get("stage"),
        "relative_strength": snap.get("relative_strength"),
        "market_regime": snap.get("regime", {}).get("state") if isinstance(snap.get("regime"), dict) else snap.get("regime"),
        "portfolio_corr": snap.get("portfolio_corr"),
        "entry_context": {
            "taker_last3": snap.get("micro", {}).get("taker_last3"),
            "rvol_1m": snap.get("micro", {}).get("rvol_1m"),
            "rvol_3m": snap.get("micro", {}).get("rvol_3m"),
            "obi": snap.get("micro", {}).get("obi"),
            "agg_cvd": snap.get("micro", {}).get("agg_cvd"),
            "spread_bps": snap.get("spread_bps"),
            "adx_15m": snap.get("adx_15m"),
            "rsi_15m": snap.get("rsi_15m"),
        },
        "peak_price": entry,
        "trough_price": entry,
        "mfe_r": 0.0,
        "mae_r": 0.0,
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
    p["peak_price"]=max(float(p.get("peak_price") or p["entry"]),float(bid))
    p["trough_price"]=min(float(p.get("trough_price") or p["entry"]),float(bid))
    r0=max(float(p.get("initial_risk_abs") or 0),1e-12)
    p["mfe_r"]=max(float(p.get("mfe_r") or 0.0),(p["peak_price"]-p["entry"])/r0)
    p["mae_r"]=max(float(p.get("mae_r") or 0.0),(p["entry"]-p["trough_price"])/r0)
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
    b1m = closed_bars(symbol, CFG["timeframes"]["micro"])
    b3m = closed_bars(symbol, CFG["timeframes"]["micro_secondary"])
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
    snap["ret_5m"] = pct_return(b1m, 5)
    snap["ret_15m"] = pct_return(b15, 1)
    snap["ret_1h"] = pct_return(b1, 1)
    snap["_bars1h"] = b1
    snap["micro_pre"] = prefilter_snapshot(b1m, b3m, CFG["momentum"])
    snap["micro_pre"]["micro_breakout_hold"] = micro_breakout_hold(
        b1m,
        lookback=int(CFG["momentum"]["recent_resistance_lookback_1m"]),
        tolerance_pct=float(CFG["momentum"]["micro_hold_tolerance_pct"]),
    )
    snap["_bars1m"] = b1m
    snap["_bars3m"] = b3m
    return snap


def enrich_microstructure(symbol, snap):
    cfg = CFG["momentum"]
    obi_samples = []
    spreads = []
    samples = int(cfg["obi_samples"])
    interval = float(cfg["obi_sample_interval_seconds"])
    for i in range(samples):
        depth = depth5(symbol)
        obi_samples.append(depth_imbalance(depth, int(cfg["obi_levels"])))
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        if bids and asks:
            spreads.append(spread_bps(float(bids[0][0]), float(asks[0][0])))
        if i < samples - 1 and interval > 0:
            time.sleep(interval)
    agg = aggtrade_delta(recent_aggtrades(symbol, 500))
    micro = combine_microstructure(snap["micro_pre"], obi_samples, spreads, agg, cfg)
    snap["micro"] = micro
    snap["eligible"] = bool(micro["stage"] == "ENTRY_CANDIDATE" and snap.get("guard_ok"))
    return snap


def validate_config():
    if CFG.get("market_type") != "spot":
        raise RuntimeError("SPOT_ONLY_CONFIG_VIOLATION")
    if CFG.get("mode") != "paper":
        raise RuntimeError("Indicator runtime is PAPER only")
    if CFG.get("engine") != "INDICATOR_ONLY_V3_EARLY_MOMENTUM":
        raise RuntimeError("Unexpected engine id")
    if int(CFG["entry"]["score_total"]) != 6 or int(CFG["entry"]["score_required"]) != 5:
        raise RuntimeError("Invalid 5-of-6 score contract")
    if float(CFG["risk"]["max_daily_loss_usdt"]) <= 0:
        raise RuntimeError("Invalid daily loss cap")
    if float(CFG["risk"]["max_risk_per_trade_usdt"]) <= 0:
        raise RuntimeError("Invalid per-trade risk")
    if not (0 < float(CFG["risk"]["max_risk_per_trade_fraction"]) <= 0.005):
        raise RuntimeError("Per-trade risk must be <= 0.5 percent")
    if CFG["risk"].get("allow_averaging_down") is not False:
        raise RuntimeError("Averaging down must stay disabled")
    if CFG["risk"].get("allow_martingale") is not False:
        raise RuntimeError("Martingale must stay disabled")
    if float(CFG["risk"].get("leverage", 1)) != 1:
        raise RuntimeError("Leverage is not allowed")
    if not (0 < float(CFG["risk"].get("max_pair_correlation", 0.85)) <= 1):
        raise RuntimeError("Invalid correlation cap")
    if int(CFG["risk"].get("consecutive_loss_cooldown_count", 3)) < 2:
        raise RuntimeError("Invalid consecutive loss cooldown")


def consecutive_loss_cooldown(state):
    risk_cfg=CFG["risk"]
    need=int(risk_cfg.get("consecutive_loss_cooldown_count",3))
    minutes=int(risk_cfg.get("consecutive_loss_cooldown_minutes",60))
    closed=list(state.get("closed_trades") or [])
    if len(closed)<need:
        return {"active":False}
    tail=closed[-need:]
    if not all(float(x.get("pnl_usdt") or 0)<0 for x in tail):
        return {"active":False}
    try:
        last=datetime.fromisoformat(str(tail[-1]["closed_at"]).replace("Z","+00:00"))
    except Exception:
        return {"active":False}
    until=last.timestamp()+minutes*60
    remaining=until-time.time()
    return {"active":remaining>0,"remaining_seconds":max(0,int(remaining)),"losses":need}


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

    # MARKET CONTEXT: use broad state and cross-sectional strength as modifiers,
    # not a pile of extra mandatory entry indicators.
    btc_snap = snapshots.get("BTCUSDT")
    btc_returns = {
        "5m": btc_snap.get("ret_5m") if btc_snap else 0.0,
        "15m": btc_snap.get("ret_15m") if btc_snap else 0.0,
        "1h": btc_snap.get("ret_1h") if btc_snap else 0.0,
    }
    rs_map = relative_strength_ranking(snapshots, btc_returns)
    for symbol, rs in rs_map.items():
        if symbol in snapshots:
            snapshots[symbol]["relative_strength"] = rs

    try:
        regime = classify_market_regime(btc1h, btc4h, snapshots)
    except Exception as exc:
        regime = {"state":"RISK_OFF","allow_new_longs":False,"risk_multiplier":0.0,"reason":f"REGIME_ERROR:{str(exc)[:120]}"}

    unavailable_count=sum(1 for x in blocked if x.get("reason")=="DATA_UNAVAILABLE")
    integrity_ratio=unavailable_count/max(1,len(symbols))
    data_integrity_ok=integrity_ratio <= float(CFG["data_integrity"]["max_unavailable_symbol_fraction"])
    if not data_integrity_ok:
        blocked.append({"reason":"GLOBAL_DATA_INTEGRITY_HALT","unavailable_fraction":integrity_ratio})

    # EARLY MOMENTUM: score all symbols cheaply on 1m/3m first, then spend
    # order-book/aggTrade calls only on the strongest few.
    micro_candidates = [
        (symbol, snap) for symbol, snap in snapshots.items()
        if symbol not in state["positions"]
        and snap.get("guard_ok")
        and float(snap.get("micro_pre", {}).get("prefilter_score", 0)) >= float(CFG["momentum"]["prefilter_min_without_orderbook"])
    ]
    micro_candidates.sort(
        key=lambda item: (
            -float(item[1]["micro_pre"].get("prefilter_score", 0)),
            -float(item[1]["micro_pre"].get("taker_latest") or 0),
            -float((item[1].get("relative_strength") or {}).get("score") or 0),
            item[0],
        )
    )
    micro_candidates = micro_candidates[: int(CFG["momentum"]["max_microstructure_candidates"])]
    if micro_candidates:
        with ThreadPoolExecutor(max_workers=min(4, len(micro_candidates))) as pool:
            futures = {pool.submit(enrich_microstructure, symbol, snap): symbol for symbol, snap in micro_candidates}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    snapshots[symbol] = future.result()
                    rs=snapshots[symbol].get("relative_strength") or {}
                    micro=snapshots[symbol].get("micro") or {}
                    # Relative strength ranks opportunity; only clearly weak coins
                    # are downgraded one stage instead of hard-rejected.
                    if rs.get("weak") and micro.get("stage")=="ENTRY_CANDIDATE":
                        micro["stage"]="ARMED"
                        snapshots[symbol]["eligible"]=False
                        micro["rs_downgrade"]=True
                    snapshots[symbol]["regime"]=regime
                except Exception as exc:
                    blocked.append({"symbol": symbol, "reason": "MICROSTRUCTURE_UNAVAILABLE", "detail": str(exc)[:160]})

    momentum_watchlist = []
    for symbol, snap in snapshots.items():
        micro = snap.get("micro")
        if micro and micro.get("stage") in {"WATCH", "ARMED", "ENTRY_CANDIDATE"}:
            momentum_watchlist.append({
                "symbol": symbol,
                "stage": micro["stage"],
                "score": micro["score"],
                "prefilter_score": micro["prefilter_score"],
                "rvol_1m": micro["rvol_1m"],
                "rvol_3m": micro["rvol_3m"],
                "trade_count_accel": micro["trade_count_accel"],
                "taker_last3": micro["taker_last3"],
                "taker_rising": micro["taker_rising"],
                "obi": micro["obi"],
                "spread_stable_or_tightening": micro["spread_stable_or_tightening"],
                "agg_cvd": micro["agg_cvd"],
                "breakout": micro["breakout"],
                "micro_breakout_hold": micro["micro_breakout_hold"],
                "vwap_distance_atr": micro["vwap_distance_atr"],
                "price_velocity": micro["price_velocity"],
                "chase_veto": micro["chase_veto"],
                "relative_strength": snap.get("relative_strength"),
                "regime": regime.get("state"),
            })
    momentum_watchlist.sort(key=lambda x: (-x["score"], x["symbol"]))

    unpriced = False
    for symbol in list(state["positions"]):
        snap = snapshots.get(symbol)
        if not snap:
            blocked.append({"symbol": symbol, "reason": "OPEN_POSITION_UNPRICED"})
            unpriced = True
            continue
        manage_position(state, symbol, snap["bid"], snap["atr_15m"])

    evidence = precision_evidence_status()
    cooldown = consecutive_loss_cooldown(state)
    signals = []
    if evidence_blocks_entries(evidence):
        blocked.append({"reason": "PRECISION_EVIDENCE_GATE", "detail": evidence})
    elif not data_integrity_ok:
        blocked.append({"reason": "DATA_INTEGRITY_VETO"})
    elif not regime.get("allow_new_longs"):
        blocked.append({"reason": "MARKET_REGIME_RISK_OFF", "detail": regime})
    elif cooldown.get("active"):
        blocked.append({"reason": "CONSECUTIVE_LOSS_COOLDOWN", "detail": cooldown})
    elif not btc.get("ok"):
        blocked.append({"reason": "BTC_REGIME_VETO", "detail": btc})
    else:
        for symbol in [x["symbol"] for x in uni]:
            if symbol in state["positions"]:
                continue
            snap = snapshots.get(symbol)
            if not snap:
                continue
            if snap.get("eligible") is True:
                signals.append(snap)

    signals.sort(key=lambda x: (
        -float(x.get("micro", {}).get("score", 0)),
        -float(x.get("micro", {}).get("taker_latest") or 0),
        -float(x.get("micro", {}).get("rvol_1m") or 0),
        -float((x.get("relative_strength") or {}).get("score") or 0),
        x["symbol"],
    ))

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
            corr=max_open_position_correlation(snap, list(state["positions"]), snapshots, points=48)
            snap["portfolio_corr"]=corr
            if corr.get("max_corr") is not None and float(corr["max_corr"])>float(CFG["risk"]["max_pair_correlation"]):
                blocked.append({"symbol":key,"reason":"CORRELATION_TOO_HIGH","detail":corr})
                state.setdefault("seen", {})[key] = snap["bar_time"]
                continue
            snap["risk_multiplier"]=float(regime.get("risk_multiplier",1.0))
            result = open_position(state, snap, snap["filters"])
            state.setdefault("seen", {})[key] = snap["bar_time"]
            if result != "PAPER_OPENED":
                blocked.append({"symbol": key, "score": snap["score"], "reason": result})

    state["signals"] = [{
        "symbol": x["symbol"],
        "momentum_stage": x.get("micro", {}).get("stage"),
        "momentum_score": x.get("micro", {}).get("score"),
        "prefilter_score": x.get("micro", {}).get("prefilter_score"),
        "rvol_1m": x.get("micro", {}).get("rvol_1m"),
        "rvol_3m": x.get("micro", {}).get("rvol_3m"),
        "trade_count_accel": x.get("micro", {}).get("trade_count_accel"),
        "taker_last3": x.get("micro", {}).get("taker_last3"),
        "obi": x.get("micro", {}).get("obi"),
        "agg_cvd": x.get("micro", {}).get("agg_cvd"),
        "micro_breakout_hold": x.get("micro", {}).get("micro_breakout_hold"),
        "context_score_15m": x["score"],
        "context_checks_15m": x["checks"],
        "bar_time": x["bar_time"],
    } for x in signals]
    state["momentum_watchlist"] = momentum_watchlist
    state["blocked"] = blocked
    state["btc_regime"] = btc
    state["market_regime"] = regime
    state["relative_strength"] = rs_map
    state["data_integrity"] = {"ok":data_integrity_ok,"unavailable_fraction":integrity_ratio}
    state["loss_cooldown"] = cooldown
    state["precision_evidence"] = evidence
    state["universe"] = [x["symbol"] for x in uni]
    state["last_run"] = now_iso()
    save_state(state)

    print(json.dumps({
        "mode": "PAPER_ONLY", "engine": CFG["engine"], "cash_usdt": state["cash_usdt"],
        "day_pnl": state["day_pnl"], "btc_regime": btc, "market_regime": regime,
        "data_integrity": state["data_integrity"], "precision_evidence": evidence, "positions": state["positions"],
        "signals": state["signals"], "momentum_watchlist": momentum_watchlist,
        "blocked": blocked, "last_run": state["last_run"],
    }, indent=2))
    return state


if __name__ == "__main__":
    main()
