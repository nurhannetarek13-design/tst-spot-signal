#!/usr/bin/env python3
"""Frozen cross-sectional Liquidity Reversal validation for V2.

This deliberately re-tests the previously promising PUBLIC_EDGE_LAB family as
ONE global Spot portfolio with max one open position, matching the user's V2
capital constraint. It does not select or optimize individual symbols.

Research only. No live authorization can be emitted by this module.
"""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

BASE_URL = "https://data-api.binance.vision"
INTERVAL = "1h"
INTERVAL_MS = 3_600_000
DAYS = 365
MIN_QV = 20_000_000.0
MAX_QV = 150_000_000.0
MAX_PRICE = 3.0
MAX_SYMBOLS = 40
STAKE = 10.0
BASE_COST_PER_SIDE = 0.0015
STRESS_COST_PER_SIDE = 0.0030
HOLD_BARS = 12
STOP_PCT = 0.03
TARGET_PCT = 0.05
Z_LOOKBACK = 30 * 24
RET_LOOKBACK = 6
VOLUME_MEDIAN_LOOKBACK = 7 * 24
Z_MAX = -2.0
RSI_MAX = 35.0
VOLUME_RATIO_MAX = 1.10
OUT = Path("validation/edges/v2-liquidity-reversal-portfolio-oos.json")

MAJORS = {"BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","LTC","BCH","LINK","AVAX","DOT"}
EXCLUDED = {"USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","AEUR","TRY","BRL","GBP","AUD","USD1","RLUSD","USDE","PAXG","XAUT"}


def api(path: str):
    request = urllib.request.Request(
        BASE_URL + path,
        headers={"User-Agent": "tst-v2-liquidity-reversal-oos/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def allowed_base(base: str) -> bool:
    return bool(
        base
        and base.isascii()
        and base.isalnum()
        and base not in EXCLUDED
        and base not in MAJORS
        and not base.endswith(("UP", "DOWN", "BULL", "BEAR"))
    )


def universe() -> list[dict]:
    info = api("/api/v3/exchangeInfo")
    tickers = {row["symbol"]: row for row in api("/api/v3/ticker/24hr")}
    rows = []
    for symbol_info in info.get("symbols", []):
        if (
            symbol_info.get("status") != "TRADING"
            or symbol_info.get("quoteAsset") != "USDT"
            or not symbol_info.get("isSpotTradingAllowed")
        ):
            continue
        base = str(symbol_info.get("baseAsset", ""))
        if not allowed_base(base):
            continue
        ticker = tickers.get(symbol_info["symbol"], {})
        price = float(ticker.get("lastPrice") or 0.0)
        qv = float(ticker.get("quoteVolume") or 0.0)
        if not (0 < price <= MAX_PRICE and MIN_QV <= qv <= MAX_QV):
            continue
        rows.append(
            {
                "symbol": symbol_info["symbol"],
                "base": base,
                "price": price,
                "quoteVolume24h": qv,
            }
        )
    rows.sort(key=lambda row: row["quoteVolume24h"], reverse=True)
    return rows[:MAX_SYMBOLS]


def klines(symbol: str, *, days: int) -> pd.DataFrame:
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    rows = []
    cursor = start
    while cursor < end:
        query = urllib.parse.urlencode(
            {
                "symbol": symbol,
                "interval": INTERVAL,
                "limit": 1000,
                "startTime": cursor,
                "endTime": end,
            }
        )
        batch = api("/api/v3/klines?" + query)
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + INTERVAL_MS
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.01)
    if len(rows) < 6_000:
        raise RuntimeError(f"{symbol}: insufficient bars {len(rows)}")
    frame = pd.DataFrame(
        rows,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_base",
            "taker_quote", "ignore",
        ],
    )
    for column in ["open", "high", "low", "close", "quote_volume", "taker_quote"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["ts"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    frame = frame.set_index("ts").sort_index()
    return frame[["open", "high", "low", "close", "quote_volume", "taker_quote"]].dropna()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gains / losses.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def enrich(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    close = out["close"]
    ret6 = close.pct_change(RET_LOOKBACK)
    mu = ret6.rolling(Z_LOOKBACK).mean()
    sigma = ret6.rolling(Z_LOOKBACK).std(ddof=0).replace(0, np.nan)
    out["z"] = (ret6 - mu) / sigma
    out["volume_ratio"] = out["quote_volume"] / out["quote_volume"].rolling(VOLUME_MEDIAN_LOOKBACK).median().replace(0, np.nan)
    out["rsi"] = rsi(close)
    out["ema24"] = close.ewm(span=24, adjust=False).mean()
    out["signal"] = (
        (out["z"] <= Z_MAX)
        & (out["volume_ratio"] <= VOLUME_RATIO_MAX)
        & (out["rsi"] <= RSI_MAX)
        & (close < out["ema24"])
    ).fillna(False)
    return out


@dataclass
class Trade:
    symbol: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    exit: float
    reason: str
    pnl: float
    z: float


def simulate(
    data: dict[str, pd.DataFrame],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cost_per_side: float,
) -> list[Trade]:
    # Global max-one-position simulation. Candidate ranking is frozen and
    # deterministic: most-negative z-score, then symbol name.
    times = sorted(
        {
            ts
            for frame in data.values()
            for ts in frame.index
            if start <= ts < end
        }
    )
    trades: list[Trade] = []
    blocked_until: pd.Timestamp | None = None
    daily_realized: dict[str, float] = {}

    for ts in times:
        if blocked_until is not None and ts <= blocked_until:
            continue
        candidates = []
        for symbol, frame in data.items():
            if ts not in frame.index:
                continue
            row = frame.loc[ts]
            if not bool(row["signal"]):
                continue
            idx = frame.index.get_loc(ts)
            if not isinstance(idx, (int, np.integer)) or idx + 1 >= len(frame):
                continue
            next_ts = frame.index[idx + 1]
            if next_ts >= end:
                continue
            candidates.append((float(row["z"]), symbol, idx))
        if not candidates:
            continue

        day = ts.date().isoformat()
        if daily_realized.get(day, 0.0) <= -2.0:
            continue

        candidates.sort(key=lambda item: (item[0], item[1]))
        z_value, symbol, idx = candidates[0]
        frame = data[symbol]
        entry_idx = idx + 1
        entry_time = frame.index[entry_idx]
        entry = float(frame["open"].iloc[entry_idx])
        if entry <= 0:
            continue
        stop = entry * (1 - STOP_PCT)
        target = entry * (1 + TARGET_PCT)
        exit_idx = min(len(frame) - 1, entry_idx + HOLD_BARS)
        exit_time = frame.index[exit_idx]
        if exit_time >= end:
            before_end = frame.index[frame.index < end]
            if len(before_end) == 0:
                continue
            exit_time = before_end[-1]
            exit_idx = frame.index.get_loc(exit_time)
        exit_price = float(frame["close"].iloc[exit_idx])
        reason = "TIME"

        for j in range(entry_idx, exit_idx + 1):
            low = float(frame["low"].iloc[j])
            high = float(frame["high"].iloc[j])
            # Conservative same-bar ordering: stop first.
            if low <= stop:
                exit_idx = j
                exit_time = frame.index[j]
                exit_price = stop
                reason = "STOP"
                break
            if high >= target:
                exit_idx = j
                exit_time = frame.index[j]
                exit_price = target
                reason = "TARGET"
                break

        gross = STAKE * (exit_price / entry - 1.0)
        fees = STAKE * cost_per_side + (STAKE * (exit_price / entry)) * cost_per_side
        pnl = gross - fees
        exit_day = exit_time.date().isoformat()
        daily_realized[exit_day] = daily_realized.get(exit_day, 0.0) + pnl
        trades.append(
            Trade(
                symbol=symbol,
                signal_time=ts,
                entry_time=entry_time,
                exit_time=exit_time,
                entry=entry,
                exit=exit_price,
                reason=reason,
                pnl=pnl,
                z=z_value,
            )
        )
        blocked_until = exit_time
    return trades


def metrics(trades: list[Trade]) -> dict:
    pnl = np.array([trade.pnl for trade in trades], dtype=float)
    if len(pnl) == 0:
        return {
            "trades": 0,
            "wins": 0,
            "winRate": None,
            "netPnlUSDT": 0.0,
            "expectancyUSDT": None,
            "profitFactor": None,
            "maxDrawdownUSDT": 0.0,
            "symbols": {},
            "reasons": {},
        }
    gross_profit = float(pnl[pnl > 0].sum()) if np.any(pnl > 0) else 0.0
    gross_loss = float(-pnl[pnl < 0].sum()) if np.any(pnl < 0) else 0.0
    equity = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.r_[0.0, equity])[:-1]
    drawdown = np.maximum(0.0, peak - equity)
    symbols = {}
    reasons = {}
    for trade in trades:
        symbols[trade.symbol] = symbols.get(trade.symbol, 0) + 1
        reasons[trade.reason] = reasons.get(trade.reason, 0) + 1
    return {
        "trades": int(len(pnl)),
        "wins": int((pnl > 0).sum()),
        "winRate": float((pnl > 0).mean()),
        "netPnlUSDT": float(pnl.sum()),
        "expectancyUSDT": float(pnl.mean()),
        "profitFactor": float(gross_profit / gross_loss) if gross_loss > 0 else None,
        "maxDrawdownUSDT": float(drawdown.max(initial=0.0)),
        "symbols": symbols,
        "reasons": reasons,
    }


def gate(result: dict, *, min_trades: int, pf_min: float) -> tuple[bool, list[str]]:
    blockers = []
    if int(result.get("trades") or 0) < min_trades:
        blockers.append("sample_insufficient")
    pf = result.get("profitFactor")
    if pf is None or float(pf) < pf_min:
        blockers.append("profit_factor_below_floor")
    expectancy = result.get("expectancyUSDT")
    if expectancy is None or float(expectancy) <= 0:
        blockers.append("expectancy_nonpositive")
    if float(result.get("maxDrawdownUSDT") or 0.0) > 2.0:
        blockers.append("max_drawdown_above_2_usdt")
    return not blockers, blockers


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=DAYS)
    parser.add_argument("--output", default=str(OUT))
    args = parser.parse_args()
    if args.days < 300 or args.days > 500:
        raise SystemExit("--days must be between 300 and 500")

    universe_rows = universe()
    data = {}
    failures = {}
    for item in universe_rows:
        symbol = item["symbol"]
        try:
            data[symbol] = enrich(klines(symbol, days=args.days))
        except Exception as exc:
            failures[symbol] = f"{type(exc).__name__}:{exc}"

    if len(data) < 6:
        raise RuntimeError(f"insufficient usable universe: {len(data)}")

    common_start = max(frame.index[0] for frame in data.values())
    common_end = min(frame.index[-1] for frame in data.values())
    usable_start = common_start + pd.Timedelta(days=35)
    span = common_end - usable_start
    discovery_end = usable_start + span * 0.50
    validation_end = usable_start + span * 0.75

    windows = {
        "discovery": (usable_start, discovery_end),
        "validation": (discovery_end, validation_end),
        "oos": (validation_end, common_end),
    }
    results = {}
    for name, (start, end) in windows.items():
        base_trades = simulate(data, start=start, end=end, cost_per_side=BASE_COST_PER_SIDE)
        stress_trades = simulate(data, start=start, end=end, cost_per_side=STRESS_COST_PER_SIDE)
        results[name] = {
            "base": metrics(base_trades),
            "stress2x": metrics(stress_trades),
        }

    disc_ok, disc_blockers = gate(results["discovery"]["base"], min_trades=20, pf_min=1.20)
    val_ok, val_blockers = gate(results["validation"]["base"], min_trades=10, pf_min=1.15)
    oos_ok, oos_blockers = gate(results["oos"]["base"], min_trades=10, pf_min=1.15)
    stress_ok, stress_blockers = gate(results["oos"]["stress2x"], min_trades=10, pf_min=1.00)
    accepted = disc_ok and val_ok and oos_ok and stress_ok

    report = {
        "strategyId": "V2_LIQUIDITY_REVERSAL_PORTFOLIO_V1",
        "family": "LIQUIDITY_REVERSAL",
        "authorization": "PAPER_ELIGIBLE_RESEARCH_ONLY" if accepted else "RESEARCH_ONLY",
        "liveTrading": False,
        "acceptedForPaperConsideration": accepted,
        "portfolio": {
            "maxOpenPositions": 1,
            "stakeUSDT": STAKE,
            "dailyLossCapUSDT": 2.0,
            "candidateRanking": "MOST_NEGATIVE_Z_THEN_SYMBOL",
        },
        "frozenDefinition": {
            "timeframe": INTERVAL,
            "retLookbackHours": RET_LOOKBACK,
            "zLookbackHours": Z_LOOKBACK,
            "zLTE": Z_MAX,
            "volumeMedianLookbackHours": VOLUME_MEDIAN_LOOKBACK,
            "volumeRatioLTE": VOLUME_RATIO_MAX,
            "rsiLTE": RSI_MAX,
            "ema": 24,
            "closeBelowEma": True,
            "holdHours": HOLD_BARS,
            "stopLossPct": STOP_PCT,
            "takeProfitPct": TARGET_PCT,
        },
        "universePolicy": {
            "currentQuoteVolumeMin": MIN_QV,
            "currentQuoteVolumeMax": MAX_QV,
            "currentPriceMax": MAX_PRICE,
            "maxSymbols": MAX_SYMBOLS,
            "excludesMajors": True,
            "asciiOnly": True,
            "survivorshipCaveat": "current tradable universe used for historical replay",
        },
        "costs": {
            "basePerSide": BASE_COST_PER_SIDE,
            "stress2xPerSide": STRESS_COST_PER_SIDE,
        },
        "split": {
            "warmupEnd": usable_start.isoformat(),
            "discoveryEnd": discovery_end.isoformat(),
            "validationEnd": validation_end.isoformat(),
            "oosEnd": common_end.isoformat(),
        },
        "results": results,
        "gates": {
            "discovery": {"pass": disc_ok, "blockers": disc_blockers},
            "validation": {"pass": val_ok, "blockers": val_blockers},
            "oos": {"pass": oos_ok, "blockers": oos_blockers},
            "oosStress2x": {"pass": stress_ok, "blockers": stress_blockers},
        },
        "universe": universe_rows,
        "symbolsUsed": sorted(data),
        "dataFailures": failures,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
