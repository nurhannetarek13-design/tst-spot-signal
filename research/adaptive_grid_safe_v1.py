#!/usr/bin/env python3
"""Research-only adaptive grid / range mean-reversion backtest.

Safety properties:
- Spot long-only, no leverage.
- Fixed 10 USDT max notional per trade.
- One open position at a time per symbol.
- Signals are formed on a completed 15m candle and entries occur at the next bar open.
- 0.10% fee per side plus configurable slippage.
- ATR hard stop, regime-breakdown exit, mean-reversion exit, and timeout.
- Last 30% of bars are reported separately as OOS.
- Never sends orders and never reads private API keys.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


@dataclass(frozen=True)
class Params:
    ema_period: int = 50
    atr_period: int = 14
    adx_period: int = 14
    adx_max: float = 24.0
    entry_atr: float = 0.85
    take_atr: float = 0.90
    stop_atr: float = 1.35
    breakdown_atr: float = 2.20
    max_bars: int = 64
    trade_usdt: float = 10.0
    min_atr_pct: float = 0.0015
    max_atr_pct: float = 0.0250
    max_ema_drop: float = 0.0120
    slope_lookback: int = 8
    fee_rate: float = 0.0010
    slippage_rate: float = 0.0005


@dataclass
class Trade:
    symbol: str
    entry_ts: str
    exit_ts: str
    entry: float
    exit: float
    qty: float
    pnl_usdt: float
    net_return_pct: float
    bars_held: int
    reason: str
    segment: str


def utc_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fetch_klines(symbol: str, start: str, end: str, interval: str = "15m") -> pd.DataFrame:
    start_ms = utc_ms(start)
    end_ms = utc_ms(end)
    rows: list[list] = []
    cursor = start_ms
    session = requests.Session()
    while cursor < end_ms:
        r = session.get(
            BINANCE_KLINES,
            params={"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms - 1, "limit": 1000},
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 1
        if next_cursor <= cursor:
            raise RuntimeError(f"Non-advancing Binance cursor for {symbol}")
        cursor = next_cursor
        time.sleep(0.04)

    if not rows:
        raise RuntimeError(f"No Binance klines returned for {symbol}")

    cols = [
        "open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
        "trades", "taker_base", "taker_quote", "ignore",
    ]
    df = pd.DataFrame(rows, columns=cols)
    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    return df[df["open_time"] < end_ms].copy()


def add_indicators(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    d = df.copy()
    prev_close = d["close"].shift(1)
    tr = pd.concat(
        [
            d["high"] - d["low"],
            (d["high"] - prev_close).abs(),
            (d["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / p.atr_period, adjust=False, min_periods=p.atr_period).mean()
    d["ema"] = d["close"].ewm(span=p.ema_period, adjust=False, min_periods=p.ema_period).mean()

    up = d["high"].diff()
    down = -d["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=d.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=d.index)
    atr_adx = tr.ewm(alpha=1 / p.adx_period, adjust=False, min_periods=p.adx_period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / p.adx_period, adjust=False, min_periods=p.adx_period).mean() / atr_adx
    minus_di = 100 * minus_dm.ewm(alpha=1 / p.adx_period, adjust=False, min_periods=p.adx_period).mean() / atr_adx
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    d["adx"] = dx.ewm(alpha=1 / p.adx_period, adjust=False, min_periods=p.adx_period).mean()
    d["atr_pct"] = d["atr"] / d["close"]
    return d


def classify_segment(i: int, split_idx: int) -> str:
    return "OOS" if i >= split_idx else "IS"


def run_symbol(symbol: str, df: pd.DataFrame, p: Params, oos_fraction: float) -> tuple[list[Trade], dict]:
    d = add_indicators(df, p)
    split_idx = int(len(d) * (1.0 - oos_fraction))
    trades: list[Trade] = []

    position = None
    pending_entry = False
    pending_exit_reason = None

    warmup = max(p.ema_period + p.slope_lookback + 2, p.atr_period * 3)
    for i in range(warmup, len(d)):
        row = d.iloc[i]
        prev = d.iloc[i - 1]
        if any(pd.isna(row[x]) for x in ["ema", "atr", "adx", "atr_pct"]):
            continue

        # Execute decisions made on the prior completed candle at this bar's open.
        if pending_exit_reason and position is not None:
            raw_exit = float(row["open"])
            exit_px = raw_exit * (1.0 - p.slippage_rate)
            gross = position["qty"] * (exit_px - position["entry"])
            fees = p.fee_rate * position["qty"] * (position["entry"] + exit_px)
            pnl = gross - fees
            net_ret = pnl / (position["qty"] * position["entry"]) * 100.0
            trades.append(
                Trade(
                    symbol=symbol,
                    entry_ts=str(position["entry_ts"]),
                    exit_ts=str(row["ts"]),
                    entry=position["entry"],
                    exit=exit_px,
                    qty=position["qty"],
                    pnl_usdt=pnl,
                    net_return_pct=net_ret,
                    bars_held=i - position["entry_i"],
                    reason=pending_exit_reason,
                    segment=position["segment"],
                )
            )
            position = None
            pending_exit_reason = None

        if pending_entry and position is None:
            entry_px = float(row["open"]) * (1.0 + p.slippage_rate)
            qty = p.trade_usdt / entry_px
            position = {
                "entry": entry_px,
                "qty": qty,
                "entry_i": i,
                "entry_ts": row["ts"],
                "entry_atr": float(prev["atr"]),
                "segment": classify_segment(i, split_idx),
            }
            pending_entry = False

        if position is not None:
            entry = position["entry"]
            e_atr = position["entry_atr"]
            hard_stop = entry - p.stop_atr * e_atr
            target = entry + p.take_atr * e_atr

            # Conservative intrabar order: if both stop and target are touched, stop wins.
            if float(row["low"]) <= hard_stop:
                exit_px = hard_stop * (1.0 - p.slippage_rate)
                gross = position["qty"] * (exit_px - entry)
                fees = p.fee_rate * position["qty"] * (entry + exit_px)
                pnl = gross - fees
                trades.append(
                    Trade(symbol, str(position["entry_ts"]), str(row["ts"]), entry, exit_px, position["qty"], pnl,
                          pnl / (position["qty"] * entry) * 100.0, i - position["entry_i"], "hard_stop",
                          position["segment"])
                )
                position = None
                continue
            if float(row["high"]) >= target:
                exit_px = target
                gross = position["qty"] * (exit_px - entry)
                fees = p.fee_rate * position["qty"] * (entry + exit_px)
                pnl = gross - fees
                trades.append(
                    Trade(symbol, str(position["entry_ts"]), str(row["ts"]), entry, exit_px, position["qty"], pnl,
                          pnl / (position["qty"] * entry) * 100.0, i - position["entry_i"], "target",
                          position["segment"])
                )
                position = None
                continue

            if float(row["close"]) >= float(row["ema"]):
                pending_exit_reason = "mean_reversion"
            elif float(row["close"]) < float(row["ema"]) - p.breakdown_atr * float(row["atr"]):
                pending_exit_reason = "range_breakdown"
            elif i - position["entry_i"] >= p.max_bars:
                pending_exit_reason = "timeout"
            continue

        # Signal generation uses only completed candles i and i-1; entry is next bar open.
        if i + 1 >= len(d):
            continue
        past_ema = float(d.iloc[i - p.slope_lookback]["ema"])
        slope_ok = float(row["ema"]) >= past_ema * (1.0 - p.max_ema_drop)
        regime_ok = (
            float(row["adx"]) <= p.adx_max
            and p.min_atr_pct <= float(row["atr_pct"]) <= p.max_atr_pct
            and slope_ok
        )
        lower_now = float(row["ema"]) - p.entry_atr * float(row["atr"])
        lower_prev = float(prev["ema"]) - p.entry_atr * float(prev["atr"])
        reclaim = float(prev["close"]) < lower_prev and float(row["close"]) > lower_now and float(row["close"]) > float(row["open"])
        if regime_ok and reclaim:
            pending_entry = True

    if position is not None:
        row = d.iloc[-1]
        exit_px = float(row["close"]) * (1.0 - p.slippage_rate)
        gross = position["qty"] * (exit_px - position["entry"])
        fees = p.fee_rate * position["qty"] * (position["entry"] + exit_px)
        pnl = gross - fees
        trades.append(
            Trade(symbol, str(position["entry_ts"]), str(row["ts"]), position["entry"], exit_px, position["qty"], pnl,
                  pnl / (position["qty"] * position["entry"]) * 100.0, len(d) - 1 - position["entry_i"], "end_of_test",
                  position["segment"])
        )

    meta = {
        "rows": len(d),
        "start": str(d["ts"].iloc[0]),
        "end": str(d["ts"].iloc[-1]),
        "oos_start": str(d["ts"].iloc[split_idx]),
    }
    return trades, meta


def metrics(trades: list[Trade], initial_cash: float = 100.0) -> dict:
    if not trades:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0, "net_pnl_usdt": 0.0,
            "avg_trade_pct": 0.0, "profit_factor": 0.0, "max_drawdown_usdt": 0.0, "max_drawdown_pct": 0.0,
        }
    pnls = np.array([t.pnl_usdt for t in trades], dtype=float)
    rets = np.array([t.net_return_pct for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_profit = wins.sum() if len(wins) else 0.0
    gross_loss = abs(losses.sum()) if len(losses) else 0.0
    pf = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)
    equity = initial_cash + np.cumsum(pnls)
    peak = np.maximum.accumulate(np.concatenate([[initial_cash], equity]))[1:]
    dd = peak - equity
    max_dd = float(dd.max()) if len(dd) else 0.0
    max_dd_pct = float((dd / peak * 100.0).max()) if len(dd) else 0.0
    return {
        "trades": int(len(trades)),
        "wins": int((pnls > 0).sum()),
        "losses": int((pnls < 0).sum()),
        "win_rate_pct": float((pnls > 0).mean() * 100.0),
        "net_pnl_usdt": float(pnls.sum()),
        "avg_trade_pct": float(rets.mean()),
        "median_trade_pct": float(np.median(rets)),
        "profit_factor": float(pf),
        "max_drawdown_usdt": max_dd,
        "max_drawdown_pct": max_dd_pct,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["LINKUSDT", "TONUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--start", default="2025-09-14")
    ap.add_argument("--end", default="2026-09-14")
    ap.add_argument("--oos-fraction", type=float, default=0.30)
    ap.add_argument("--output", default="artifacts/adaptive-grid-safe-v1/report.json")
    args = ap.parse_args()

    p = Params()
    report = {
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "strategy": "AdaptiveGridSafeSpotV1",
        "timeframe": "15m",
        "params": asdict(p),
        "window": {"start": args.start, "end": args.end, "oos_fraction": args.oos_fraction},
        "symbols": {},
    }
    all_trades: list[Trade] = []

    for symbol in args.symbols:
        print(f"Fetching {symbol}...")
        df = fetch_klines(symbol, args.start, args.end)
        trades, meta = run_symbol(symbol, df, p, args.oos_fraction)
        all_trades.extend(trades)
        is_trades = [t for t in trades if t.segment == "IS"]
        oos_trades = [t for t in trades if t.segment == "OOS"]
        report["symbols"][symbol] = {
            "meta": meta,
            "overall": metrics(trades),
            "IS": metrics(is_trades),
            "OOS": metrics(oos_trades),
            "trades_detail": [asdict(t) for t in trades],
        }
        print(symbol, json.dumps(report["symbols"][symbol]["OOS"], indent=2))

    report["aggregate"] = {
        "overall": metrics(all_trades),
        "IS": metrics([t for t in all_trades if t.segment == "IS"]),
        "OOS": metrics([t for t in all_trades if t.segment == "OOS"]),
    }

    # Promotion gate is intentionally strict and cannot enable live trading.
    eligible = []
    for symbol, r in report["symbols"].items():
        o = r["OOS"]
        if o["trades"] >= 20 and o["profit_factor"] >= 1.20 and o["avg_trade_pct"] > 0 and o["max_drawdown_pct"] <= 2.0:
            eligible.append(symbol)
    report["promotionGate"] = {
        "decision": "REJECT" if len(eligible) < 2 else "RESEARCH_PASS_NOT_LIVE",
        "eligibleSymbols": eligible,
        "requiresAtLeastTwoSymbols": True,
        "canEnableLiveTrading": False,
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"aggregate": report["aggregate"], "promotionGate": report["promotionGate"]}, indent=2))


if __name__ == "__main__":
    main()
