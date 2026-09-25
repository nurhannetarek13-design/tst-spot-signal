#!/usr/bin/env python3
"""Research-only multi-symbol discovery for Indicator Fusion V1.

One preregistered configuration is evaluated across an ex-ante liquidity basket.
There is no parameter search, no per-symbol tuning, and no automatic live promotion.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from research.indicator_fusion_v1 import DEFAULT_PARAMS, compute_indicator_fusion, contract, risk_geometry


BASE_URL = "https://data-api.binance.vision"
INTERVAL = "1h"
DAYS = 365
MAX_SYMBOLS = 40
MIN_QV = 20_000_000.0
MAX_QV = 500_000_000.0
STAKE = 5.5
BASE_COST_PER_SIDE = 0.0015
STRESS_COST_PER_SIDE = 0.0030
OUT = pathlib.Path("validation/edges/indicator-fusion-v1-latest.json")
STRATEGY_ID = "TST_INDICATOR_FUSION_DISCOVERY_V1"

MAJORS = {"BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "TRX", "LTC", "BCH", "LINK", "AVAX", "DOT"}
EXCLUDED = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "AEUR", "TRY", "BRL", "GBP", "AUD", "USD1", "RLUSD", "USDE", "PAXG", "XAUT"}


def api(path: str):
    req = urllib.request.Request(BASE_URL + path, headers={"User-Agent": "tst-indicator-fusion-v1/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def universe() -> list[dict]:
    info = api("/api/v3/exchangeInfo")
    tick = {x["symbol"]: x for x in api("/api/v3/ticker/24hr")}
    rows = []
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT" or not s.get("isSpotTradingAllowed"):
            continue
        base = s.get("baseAsset", "")
        if not base or base in EXCLUDED or base in MAJORS or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        t = tick.get(s["symbol"], {})
        qv = float(t.get("quoteVolume") or 0)
        price = float(t.get("lastPrice") or 0)
        if not (price > 0 and MIN_QV <= qv <= MAX_QV):
            continue
        rows.append({"symbol": s["symbol"], "base": base, "price": price, "quoteVolume24h": qv})
    rows.sort(key=lambda x: x["quoteVolume24h"], reverse=True)
    return rows[:MAX_SYMBOLS]


def klines(symbol: str) -> pd.DataFrame:
    end = int(time.time() * 1000)
    start = end - DAYS * 86_400_000
    rows = []
    cur = start
    while cur < end:
        q = urllib.parse.urlencode(
            {"symbol": symbol, "interval": INTERVAL, "limit": 1000, "startTime": cur, "endTime": end}
        )
        batch = api("/api/v3/klines?" + q)
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1][0]) + 3_600_000
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.01)
    if len(rows) < 4000:
        raise RuntimeError(f"{symbol}: insufficient bars {len(rows)}")
    df = pd.DataFrame(
        rows,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
        ],
    )
    for c in ["open", "high", "low", "close", "volume", "quote_volume", "taker_quote"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("ts")[["open", "high", "low", "close", "volume", "quote_volume", "taker_quote"]].dropna()


def backtest_slice(
    symbol: str,
    df: pd.DataFrame,
    features: pd.DataFrame,
    start: int,
    end: int,
    cost_per_side: float,
) -> list[dict]:
    trades = []
    i = max(0, int(start))
    end = min(int(end), len(df))
    while i < end - 1:
        if not bool(features["enter"].iloc[i]):
            i += 1
            continue

        entry_i = i + 1
        if entry_i >= end:
            break
        entry = float(df["open"].iloc[entry_i])
        if entry <= 0:
            i += 1
            continue

        atr = float(features["atr_pct"].iloc[i])
        if not np.isfinite(atr) or atr <= 0:
            i += 1
            continue
        risk_pct, target_pct, hold = risk_geometry(atr, DEFAULT_PARAMS)
        stop = entry * (1.0 - risk_pct)
        target = entry * (1.0 + target_pct)
        exit_i = min(end - 1, entry_i + hold)
        exit_px = float(df["close"].iloc[exit_i])
        reason = "TIME"

        for j in range(entry_i, exit_i + 1):
            lo = float(df["low"].iloc[j])
            hi = float(df["high"].iloc[j])
            if lo <= stop:
                exit_i, exit_px, reason = j, stop, "STOP"
                break
            if hi >= target:
                exit_i, exit_px, reason = j, target, "TARGET"
                break

        gross = STAKE * (exit_px / entry - 1.0)
        fees = STAKE * cost_per_side + (STAKE * (exit_px / entry)) * cost_per_side
        pnl = gross - fees
        trades.append(
            {
                "symbol": symbol,
                "entry": df.index[entry_i].isoformat(),
                "exit": df.index[exit_i].isoformat(),
                "pnl": float(pnl),
                "reason": reason,
                "signalScore": float(features["score"].iloc[i]),
                "families": int(features["families"].iloc[i]),
                "riskPct": float(risk_pct),
                "targetPct": float(target_pct),
            }
        )
        i = exit_i + 1
    return trades


def metrics(trades: list[dict]) -> dict:
    ordered = sorted(trades, key=lambda x: (x["exit"], x["symbol"]))
    p = np.asarray([float(x["pnl"]) for x in ordered], dtype=float)
    if len(p) == 0:
        return {
            "trades": 0, "wins": 0, "winRate": 0.0, "netPnlUSDT": 0.0,
            "expectancyUSDT": 0.0, "profitFactor": 0.0, "maxDrawdownUSDT": 0.0,
        }
    gp = float(p[p > 0].sum()) if np.any(p > 0) else 0.0
    gl = float(-p[p < 0].sum()) if np.any(p < 0) else 0.0
    eq = np.cumsum(p)
    peak = np.maximum.accumulate(np.r_[0.0, eq])[:-1]
    return {
        "trades": int(len(p)),
        "wins": int((p > 0).sum()),
        "winRate": float((p > 0).mean()),
        "netPnlUSDT": float(p.sum()),
        "expectancyUSDT": float(p.mean()),
        "profitFactor": float(gp / gl) if gl > 0 else (999.0 if gp > 0 else 0.0),
        "maxDrawdownUSDT": float(np.maximum(0, peak - eq).max(initial=0.0)),
    }


def split_ranges(n: int) -> dict[str, tuple[int, int]]:
    a = int(n * 0.60)
    b = int(n * 0.80)
    return {"discovery": (0, a), "validation": (a, b), "oos": (b, n)}


def passes_phase(row: dict, *, min_trades: int, min_pf: float, max_dd: float) -> bool:
    return bool(
        row["trades"] >= min_trades
        and row["expectancyUSDT"] > 0
        and row["profitFactor"] >= min_pf
        and row["maxDrawdownUSDT"] <= max_dd
    )


def main():
    rows = universe()
    data: dict[str, pd.DataFrame] = {}
    failures: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(klines, x["symbol"]): x["symbol"] for x in rows}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                data[symbol] = future.result()
            except Exception as exc:
                failures[symbol] = str(exc)

    ordered_symbols = [x["symbol"] for x in rows if x["symbol"] in data]
    pooled = {
        "discovery": {"base": [], "stress2x": []},
        "validation": {"base": [], "stress2x": []},
        "oos": {"base": [], "stress2x": []},
    }
    per_symbol = {}

    for symbol in ordered_symbols:
        df = data[symbol]
        f = compute_indicator_fusion(df, DEFAULT_PARAMS)
        ranges = split_ranges(len(df))
        per_symbol[symbol] = {}
        for name, (start, end) in ranges.items():
            base = backtest_slice(symbol, df, f, start, end, BASE_COST_PER_SIDE)
            stress = backtest_slice(symbol, df, f, start, end, STRESS_COST_PER_SIDE)
            pooled[name]["base"].extend(base)
            pooled[name]["stress2x"].extend(stress)
            per_symbol[symbol][name] = {"base": metrics(base), "stress2x": metrics(stress)}

    aggregate = {}
    for name in ["discovery", "validation", "oos"]:
        aggregate[name] = {
            "base": metrics(pooled[name]["base"]),
            "stress2x": metrics(pooled[name]["stress2x"]),
        }

    oos_profitable = [
        s for s in ordered_symbols
        if per_symbol[s]["oos"]["stress2x"]["trades"] > 0
        and per_symbol[s]["oos"]["stress2x"]["netPnlUSDT"] > 0
    ]
    oos_counts = {
        s: per_symbol[s]["oos"]["base"]["trades"]
        for s in ordered_symbols
    }
    total_oos = max(1, sum(oos_counts.values()))
    top_share = max(oos_counts.values(), default=0) / total_oos

    min_profitable = max(4, len(ordered_symbols) // 6)
    base_gate = (
        len(ordered_symbols) >= 12
        and passes_phase(aggregate["discovery"]["base"], min_trades=80, min_pf=1.10, max_dd=5.0)
        and passes_phase(aggregate["validation"]["base"], min_trades=30, min_pf=1.05, max_dd=3.0)
        and passes_phase(aggregate["oos"]["base"], min_trades=30, min_pf=1.05, max_dd=3.0)
        and passes_phase(aggregate["validation"]["stress2x"], min_trades=30, min_pf=1.00, max_dd=4.0)
        and passes_phase(aggregate["oos"]["stress2x"], min_trades=30, min_pf=1.00, max_dd=4.0)
        and len(oos_profitable) >= min_profitable
        and top_share <= 0.35
    )

    cfg = contract()
    report = {
        "engine": "INDICATOR_FUSION_DISCOVERY_V1",
        "strategyId": STRATEGY_ID,
        "status": "BASKET_EDGE_FOUND" if base_gate else "NO_BASKET_EDGE_PASS",
        "pass": bool(base_gate),
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "automaticPromotion": False,
        "parameterSearch": False,
        "timeframe": INTERVAL,
        "days": DAYS,
        "universeMode": "LIQUID_SMALL_MID_CAP_USDT_EX_ANTE",
        "universe": rows,
        "symbolsTested": ordered_symbols,
        "loadedSymbols": len(ordered_symbols),
        "dataFailures": failures,
        "config": cfg,
        "aggregate": aggregate,
        "breadth": {
            "oosProfitableSymbolsStress": len(oos_profitable),
            "minimumRequired": min_profitable,
            "profitableSymbols": oos_profitable,
            "largestOosTradeShare": float(top_share),
            "largestAllowedTradeShare": 0.35,
        },
        "perSymbol": per_symbol,
        "validatorBasketSymbols": ordered_symbols[:5],
        "candidateSpec": {
            "source": "INDICATOR_FUSION_DISCOVERY_V1",
            "family": "INDICATOR_FUSION_V1",
            "timeframe": INTERVAL,
            "symbols": ordered_symbols[:5],
            "params": dict(DEFAULT_PARAMS),
            "historicalScope": "CANDLE_FUSION_ONLY",
            "historicalExcludes": ["L2_ORDER_BOOK_CONFIRMATION"],
            "eligibleForIndependentValidation": bool(base_gate),
            "liveTrading": False,
        },
        "costs": {
            "basePerSide": BASE_COST_PER_SIDE,
            "stressPerSide": STRESS_COST_PER_SIDE,
        },
        "split": {"discovery": 0.60, "validation": 0.20, "oos": 0.20},
        "notes": [
            "One preregistered Indicator Fusion configuration; no threshold or weight search.",
            "Basket membership is selected ex ante by current quote-volume rank, not by backtest outcome.",
            "Signals enter on the next bar open; same-bar stop/target collisions resolve pessimistically to stop.",
            "Historical scoring uses candle taker flow only. L2/order-book confirmation is excluded.",
            "A pass authorizes independent validators and forward paper only; never live trading.",
        ],
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    print(json.dumps({
        "status": report["status"],
        "loadedSymbols": report["loadedSymbols"],
        "aggregate": aggregate,
        "breadth": report["breadth"],
        "validatorBasketSymbols": report["validatorBasketSymbols"],
    }, indent=2))


if __name__ == "__main__":
    main()
