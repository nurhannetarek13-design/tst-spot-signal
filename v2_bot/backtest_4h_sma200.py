from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass

import httpx

BASE_URL = "https://data-api.binance.vision"
SYMBOL = "BTCUSDT"
INTERVAL = "4h"
START_MS = 1546300800000  # 2019-01-01 UTC
FEE_RATE = 0.001          # 0.10% each side
SLIPPAGE_RATE = 0.0005    # 5 bps each side
QUOTE_SIZE = 10.0
SMA_N = 200


def fetch_bars() -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    start = START_MS
    now_ms = int(time.time() * 1000)
    with httpx.Client(timeout=30.0) as client:
        while start < now_ms:
            r = client.get(
                f"{BASE_URL}/api/v3/klines",
                params={"symbol": SYMBOL, "interval": INTERVAL, "limit": 1000, "startTime": start},
            )
            r.raise_for_status()
            rows = r.json()
            if not rows:
                break
            for row in rows:
                close_time = int(row[6])
                if close_time >= now_ms:
                    continue
                out.append({
                    "open_time": float(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "close_time": float(row[6]),
                })
            nxt = int(rows[-1][6]) + 1
            if nxt <= start:
                break
            start = nxt
            if len(rows) < 1000:
                break
    if len(out) < SMA_N + 10:
        raise RuntimeError(f"insufficient bars: {len(out)}")
    return out


@dataclass
class Trade:
    entry_time: float
    exit_time: float
    entry: float
    exit: float
    pnl: float
    ret: float


def run_window(bars: list[dict[str, float]], start_i: int, end_i: int) -> dict:
    closes = [b["close"] for b in bars]
    sma = [math.nan] * len(bars)
    rolling = sum(closes[:SMA_N])
    sma[SMA_N - 1] = rolling / SMA_N
    for i in range(SMA_N, len(bars)):
        rolling += closes[i] - closes[i - SMA_N]
        sma[i] = rolling / SMA_N

    trades: list[Trade] = []
    in_pos = False
    entry_px = entry_time = 0.0
    equity = 0.0
    peak = 0.0
    max_dd = 0.0

    i = max(start_i, SMA_N)
    while i < min(end_i, len(bars) - 1):
        prev_above = closes[i - 1] > sma[i - 1]
        curr_above = closes[i] > sma[i]
        next_open = bars[i + 1]["open"]
        next_time = bars[i + 1]["open_time"]

        if not in_pos and (not prev_above) and curr_above:
            entry_px = next_open * (1.0 + SLIPPAGE_RATE)
            entry_time = next_time
            in_pos = True
        elif in_pos and prev_above and (not curr_above):
            exit_px = next_open * (1.0 - SLIPPAGE_RATE)
            gross_ret = exit_px / entry_px - 1.0
            net_ret = gross_ret - 2.0 * FEE_RATE
            pnl = QUOTE_SIZE * net_ret
            trades.append(Trade(entry_time, next_time, entry_px, exit_px, pnl, net_ret))
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
            in_pos = False
        i += 1

    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    gp = sum(wins)
    gl = -sum(losses)
    pf = gp / gl if gl > 0 else None
    expectancy = sum(t.pnl for t in trades) / len(trades) if trades else None
    win_rate = len(wins) / len(trades) if trades else None
    net = sum(t.pnl for t in trades)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "profit_factor": pf,
        "expectancy_usdt": expectancy,
        "net_pnl_usdt": net,
        "max_drawdown_usdt": max_dd,
        "open_at_end": in_pos,
    }


def main() -> None:
    bars = fetch_bars()
    split = int(len(bars) * 0.70)
    discovery = run_window(bars, 0, split)
    oos = run_window(bars, split, len(bars))
    full = run_window(bars, 0, len(bars))

    def passes(m: dict, min_trades: int) -> bool:
        pf = m["profit_factor"]
        ex = m["expectancy_usdt"]
        return bool(
            m["trades"] >= min_trades
            and pf is not None and pf >= 1.20
            and ex is not None and ex > 0
            and m["net_pnl_usdt"] > 0
            and m["max_drawdown_usdt"] <= 2.0
        )

    verdict = passes(discovery, 15) and passes(oos, 8)
    print(json.dumps({
        "strategy_id": "external_4h_sma200",
        "symbol": SYMBOL,
        "timeframe": INTERVAL,
        "bars": len(bars),
        "cost_model": {"fee_each_side": FEE_RATE, "slippage_each_side": SLIPPAGE_RATE, "quote_size_usdt": QUOTE_SIZE},
        "discovery": discovery,
        "oos": oos,
        "full": full,
        "promotion_gate_passed": verdict,
        "live_trading": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
