#!/usr/bin/env python3
"""Research-only regime-stratified validation for the frozen liquidity-reversal edge.

The reversal definition is frozen. Only an exogenous BTC regime label is used to
stratify the same signals. A strategy can be considered for Paper only when the
same regime survives Discovery, Validation, untouched OOS and 2x-friction OOS.
No live authorization is possible from this script.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import random
import statistics
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

BASE_URL = "https://data-api.binance.vision"
DAYS = 365
INTERVAL_MS = 3_600_000
MIN_QV = 20_000_000.0
MAX_SYMBOLS = 35
STAKE = 10.0
DAILY_LOSS_CAP = 2.0
BASE_COST_PER_SIDE = 0.0015
STRESS_COST_PER_SIDE = 0.0030
OUT = Path(os.environ.get("V2_REGIME_REVERSAL_OUT", "v2-regime-stratified-reversal.json"))
RNG = random.Random(20260912)

MAJORS = {"BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "TRX", "LTC", "BCH", "LINK", "AVAX", "DOT"}
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "AEUR", "TRY", "BRL", "GBP", "AUD", "USD1", "RLUSD", "USDE", "U", "PAXG", "XAUT"}
REGIMES = ("bull_trend", "bear_trend", "recovery", "panic", "high_vol", "range")


def api(path: str):
    req = urllib.request.Request(BASE_URL + path, headers={"User-Agent": "tst-v2-regime-reversal/1.0"})
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


def allowed_base(base: str) -> bool:
    if not base or not base.isascii() or base in MAJORS or base in STABLES:
        return False
    return not base.endswith(("UP", "DOWN", "BULL", "BEAR"))


def universe() -> list[dict]:
    info = api("/api/v3/exchangeInfo")
    tickers = {row["symbol"]: row for row in api("/api/v3/ticker/24hr")}
    rows = []
    for row in info.get("symbols", []):
        if row.get("status") != "TRADING" or row.get("quoteAsset") != "USDT" or not row.get("isSpotTradingAllowed"):
            continue
        base = str(row.get("baseAsset", ""))
        if not allowed_base(base):
            continue
        ticker = tickers.get(row["symbol"], {})
        qv = float(ticker.get("quoteVolume") or 0)
        if qv < MIN_QV:
            continue
        rows.append({
            "symbol": row["symbol"],
            "base": base,
            "price": float(ticker.get("lastPrice") or 0),
            "quoteVolume24h": qv,
        })
    rows.sort(key=lambda item: item["quoteVolume24h"], reverse=True)
    return rows[:MAX_SYMBOLS]


def klines(symbol: str) -> list[dict]:
    end = int(time.time() * 1000)
    start = end - DAYS * 86_400_000
    rows = []
    cursor = start
    while cursor < end:
        query = urllib.parse.urlencode({"symbol": symbol, "interval": "1h", "limit": 1000, "startTime": cursor, "endTime": end})
        batch = api("/api/v3/klines?" + query)
        if not batch:
            break
        for x in batch:
            rows.append({
                "open_time": int(x[0]), "open": float(x[1]), "high": float(x[2]), "low": float(x[3]),
                "close": float(x[4]), "quote_volume": float(x[7]),
            })
        nxt = int(batch[-1][0]) + INTERVAL_MS
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.005)
    dedup = {row["open_time"]: row for row in rows}
    out = [dedup[key] for key in sorted(dedup)]
    if len(out) < 3500:
        raise RuntimeError(f"{symbol}: insufficient bars {len(out)}")
    return out


def ema(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    out = []
    current = values[0]
    for value in values:
        current = alpha * value + (1.0 - alpha) * current
        out.append(current)
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    out = [50.0] * len(values)
    gains = [0.0] * len(values)
    losses = [0.0] * len(values)
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains[i] = max(d, 0.0)
        losses[i] = max(-d, 0.0)
    if len(values) <= period:
        return out
    avg_g = sum(gains[1:period + 1]) / period
    avg_l = sum(losses[1:period + 1]) / period
    out[period] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(period + 1, len(values)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def rolling_mean(values: list[float], n: int) -> list[float | None]:
    out = [None] * len(values)
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def rolling_median(values: list[float | None], n: int) -> list[float | None]:
    out = [None] * len(values)
    for i in range(n - 1, len(values)):
        sample = [x for x in values[i - n + 1:i + 1] if x is not None]
        if len(sample) >= max(10, n // 2):
            out[i] = statistics.median(sample)
    return out


def build_btc_regimes(rows: list[dict]) -> dict[int, str]:
    closes = [row["close"] for row in rows]
    e24, e72 = ema(closes, 24), ema(closes, 72)
    ranges = [(row["high"] - row["low"]) / max(row["close"], 1e-12) for row in rows]
    vol24 = rolling_mean(ranges, 24)
    vol_baseline = rolling_median(vol24, 24 * 30)
    regimes = {}
    for i in range(72, len(rows)):
        ret6 = closes[i] / closes[i - 6] - 1.0
        ret24 = closes[i] / closes[i - 24] - 1.0
        current_vol = vol24[i] or 0.0
        baseline = vol_baseline[i] or 0.0
        if ret6 <= -0.025 or ret24 <= -0.055:
            label = "panic"
        elif ret6 >= 0.015 and closes[i] > e24[i]:
            label = "recovery"
        elif e24[i] > e72[i] and closes[i] > e24[i]:
            label = "bull_trend"
        elif e24[i] < e72[i] and closes[i] < e24[i]:
            label = "bear_trend"
        elif baseline > 0 and current_vol >= 1.5 * baseline:
            label = "high_vol"
        else:
            label = "range"
        regimes[rows[i]["open_time"]] = label
    return regimes


def signal_events(symbol: str, rows: list[dict], btc_regimes: dict[int, str]) -> list[dict]:
    closes = [row["close"] for row in rows]
    qv = [row["quote_volume"] for row in rows]
    e24 = ema(closes, 24)
    r = rsi(closes, 14)
    events = []
    ret6 = [None] * len(rows)
    for i in range(6, len(rows)):
        ret6[i] = closes[i] / closes[i - 6] - 1.0
    for i in range(720, len(rows) - 13):
        if rows[i]["open_time"] not in btc_regimes or ret6[i] is None:
            continue
        history = [x for x in ret6[i - 719:i + 1] if x is not None]
        if len(history) < 700:
            continue
        mu = statistics.fmean(history)
        sigma = statistics.pstdev(history)
        if sigma <= 0:
            continue
        z = (ret6[i] - mu) / sigma
        vol_med = statistics.median(qv[i - 167:i + 1])
        vol_ratio = qv[i] / vol_med if vol_med > 0 else math.inf
        if not (z <= -2.0 and vol_ratio <= 1.10 and r[i] <= 35.0 and closes[i] < e24[i]):
            continue
        events.append({
            "symbol": symbol,
            "signal_i": i,
            "signal_time": rows[i]["open_time"],
            "regime": btc_regimes[rows[i]["open_time"]],
            "z": z,
            "rows": rows,
        })
    return events


def run_portfolio(events: list[dict], start_ms: int, end_ms: int, regime: str, cost_per_side: float) -> list[dict]:
    eligible = [e for e in events if start_ms <= e["signal_time"] < end_ms and e["regime"] == regime]
    by_time: dict[int, list[dict]] = defaultdict(list)
    for event in eligible:
        by_time[event["signal_time"]].append(event)
    trades = []
    busy_until = -1
    daily_pnl: dict[str, float] = defaultdict(float)
    for ts in sorted(by_time):
        if ts <= busy_until:
            continue
        day = dt.datetime.fromtimestamp(ts / 1000, tz=dt.timezone.utc).date().isoformat()
        if daily_pnl[day] <= -DAILY_LOSS_CAP:
            continue
        event = sorted(by_time[ts], key=lambda e: (e["z"], e["symbol"]))[0]
        rows, i = event["rows"], event["signal_i"]
        entry_i = i + 1
        entry = rows[entry_i]["open"]
        stop = entry * 0.97
        target = entry * 1.05
        exit_i = min(len(rows) - 1, entry_i + 12)
        exit_px = rows[exit_i]["close"]
        reason = "TIME"
        for j in range(entry_i, exit_i + 1):
            if rows[j]["low"] <= stop:
                exit_i, exit_px, reason = j, stop, "STOP"
                break
            if rows[j]["high"] >= target:
                exit_i, exit_px, reason = j, target, "TARGET"
                break
        gross = STAKE * (exit_px / entry - 1.0)
        fees = STAKE * cost_per_side + (STAKE * exit_px / entry) * cost_per_side
        pnl = gross - fees
        exit_time = rows[exit_i]["open_time"]
        trades.append({"symbol": event["symbol"], "entry_time": rows[entry_i]["open_time"], "exit_time": exit_time, "pnl": pnl, "reason": reason})
        daily_pnl[day] += pnl
        busy_until = exit_time
    return trades


def metrics(trades: list[dict]) -> dict:
    pnls = [float(t["pnl"]) for t in trades]
    if not pnls:
        return {"trades": 0, "wins": 0, "winRate": 0.0, "netPnlUSDT": 0.0, "expectancyUSDT": 0.0, "profitFactor": None, "maxDrawdownUSDT": 0.0}
    gp = sum(x for x in pnls if x > 0)
    gl = -sum(x for x in pnls if x < 0)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in pnls:
        equity += x
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trades": len(pnls),
        "wins": sum(x > 0 for x in pnls),
        "winRate": sum(x > 0 for x in pnls) / len(pnls),
        "netPnlUSDT": sum(pnls),
        "expectancyUSDT": statistics.fmean(pnls),
        "profitFactor": gp / gl if gl > 0 else None,
        "maxDrawdownUSDT": max_dd,
    }


def bootstrap_p_positive(trades: list[dict], samples: int = 2000) -> float:
    pnls = [float(t["pnl"]) for t in trades]
    if len(pnls) < 8:
        return 1.0
    nonpositive = 0
    for _ in range(samples):
        mean = statistics.fmean(RNG.choice(pnls) for _ in pnls)
        if mean <= 0:
            nonpositive += 1
    return (nonpositive + 1) / (samples + 1)


def bh_qvalues(pvals: dict[str, float]) -> dict[str, float]:
    ordered = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(ordered)
    q = {}
    running = 1.0
    for rank in range(m, 0, -1):
        name, p = ordered[rank - 1]
        running = min(running, p * m / rank)
        q[name] = min(1.0, running)
    return q


def gate(base: dict, stress: dict, minimum: int) -> tuple[bool, list[str]]:
    blockers = []
    if base["trades"] < minimum:
        blockers.append("sample_insufficient")
    if base["profitFactor"] is None or base["profitFactor"] < 1.15:
        blockers.append("base_pf_below_1_15_or_unproven")
    if base["expectancyUSDT"] <= 0:
        blockers.append("base_expectancy_nonpositive")
    if stress["profitFactor"] is None or stress["profitFactor"] < 1.0:
        blockers.append("stress_pf_below_1_or_unproven")
    if stress["expectancyUSDT"] <= 0:
        blockers.append("stress_expectancy_nonpositive")
    return not blockers, blockers


rows = universe()
data: dict[str, list[dict]] = {}
failures = {}
for symbol in ["BTCUSDT"] + [x["symbol"] for x in rows]:
    try:
        data[symbol] = klines(symbol)
    except Exception as exc:
        failures[symbol] = f"{type(exc).__name__}:{exc}"

btc = data["BTCUSDT"]
btc_regimes = build_btc_regimes(btc)
all_events = []
for item in rows:
    symbol = item["symbol"]
    if symbol in data:
        all_events.extend(signal_events(symbol, data[symbol], btc_regimes))

start = btc[0]["open_time"]
end = btc[-1]["open_time"] + INTERVAL_MS
span = end - start
warmup_end = start + int(span * 0.20)
discovery_end = start + int(span * 0.60)
validation_end = start + int(span * 0.80)
periods = {
    "discovery": (warmup_end, discovery_end),
    "validation": (discovery_end, validation_end),
    "oos": (validation_end, end),
}

report_regimes = {}
discovery_pvals = {}
for regime in REGIMES:
    sections = {}
    for name, (a, b) in periods.items():
        base_trades = run_portfolio(all_events, a, b, regime, BASE_COST_PER_SIDE)
        stress_trades = run_portfolio(all_events, a, b, regime, STRESS_COST_PER_SIDE)
        base, stress = metrics(base_trades), metrics(stress_trades)
        passed, blockers = gate(base, stress, 15 if name == "discovery" else 10)
        sections[name] = {"base": base, "stress2x": stress, "gatePass": passed, "blockers": blockers}
        if name == "discovery":
            discovery_pvals[regime] = bootstrap_p_positive(base_trades)
    report_regimes[regime] = sections

qvalues = bh_qvalues(discovery_pvals)
accepted = []
for regime, sections in report_regimes.items():
    q = qvalues.get(regime, 1.0)
    sections["discovery"]["bootstrapP"] = discovery_pvals.get(regime, 1.0)
    sections["discovery"]["bhQ"] = q
    if q > 0.10:
        sections["discovery"]["gatePass"] = False
        sections["discovery"]["blockers"].append("discovery_bh_q_above_0_10")
    if all(sections[name]["gatePass"] for name in ("discovery", "validation", "oos")):
        accepted.append(regime)

report = {
    "strategyId": "V2_LIQUIDITY_REVERSAL_REGIME_V2",
    "family": "LIQUIDITY_REVERSAL",
    "authorization": "PAPER_CONSIDERATION_ONLY" if accepted else "RESEARCH_ONLY",
    "liveTrading": False,
    "acceptedRegimesForPaperConsideration": accepted,
    "frozenSignal": {
        "timeframe": "1h", "retLookbackHours": 6, "zLookbackHours": 720, "zLTE": -2.0,
        "volumeMedianLookbackHours": 168, "volumeRatioLTE": 1.10, "rsiLTE": 35.0,
        "ema": 24, "closeBelowEma": True, "holdHours": 12, "stopLossPct": 0.03, "takeProfitPct": 0.05,
    },
    "regimeDefinition": {
        "source": "BTCUSDT_ONLY_PRE_SIGNAL", "labels": list(REGIMES),
        "panic": "BTC 6h<=-2.5% or 24h<=-5.5%",
        "recovery": "BTC 6h>=+1.5% and close>EMA24",
        "bull_trend": "EMA24>EMA72 and close>EMA24",
        "bear_trend": "EMA24<EMA72 and close<EMA24",
        "high_vol": "24h mean range >=1.5x trailing-30d median",
        "range": "otherwise",
    },
    "portfolio": {"maxOpenPositions": 1, "stakeUSDT": STAKE, "dailyLossCapUSDT": DAILY_LOSS_CAP, "ranking": "MOST_NEGATIVE_Z_THEN_SYMBOL"},
    "multipleTesting": {"method": "bootstrap_mean_then_BH_FDR", "discoveryQMax": 0.10, "bootstrapSamples": 2000},
    "costs": {"basePerSide": BASE_COST_PER_SIDE, "stress2xPerSide": STRESS_COST_PER_SIDE},
    "split": {"warmupPct": 0.20, "discoveryPct": 0.40, "validationPct": 0.20, "oosPct": 0.20},
    "universePolicy": {"currentQuoteVolumeMin": MIN_QV, "maxSymbols": MAX_SYMBOLS, "excludesMajors": True, "excludesStables": True, "asciiOnly": True, "survivorshipCaveat": "current tradable universe used for historical replay"},
    "regimes": report_regimes,
    "universe": rows,
    "eventCount": len(all_events),
    "dataFailures": failures,
    "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
}
OUT.write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
