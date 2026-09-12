from __future__ import annotations

import argparse
import bisect
import json
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import Settings
from .strategy import Candidate, evaluate_candidate


INTERVAL_MS = 15 * 60 * 1000
PUBLIC_BASE_URLS = (
    "https://api.binance.com",
    "https://data-api.binance.vision",
)
DEFAULT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "LINKUSDT",
)


@dataclass(frozen=True)
class ReplayTrade:
    symbol: str
    setup: str
    signal_open_time: int
    entry_open_time: int
    exit_open_time: int | None
    entry_price: float
    exit_price: float | None
    reason: str
    pnl_usdt: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "setup": self.setup,
            "signal_open_time": self.signal_open_time,
            "entry_open_time": self.entry_open_time,
            "exit_open_time": self.exit_open_time,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "reason": self.reason,
            "pnl_usdt": self.pnl_usdt,
        }


def _parse_kline(row: list[Any]) -> dict[str, float]:
    return {
        "open_time": float(row[0]),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
        "close_time": float(row[6]),
        "quote_volume": float(row[7]),
        "trades": float(row[8]),
        "taker_buy_base": float(row[9]),
        "taker_buy_quote": float(row[10]),
    }


def fetch_spot_15m(
    symbol: str,
    *,
    start_ms: int,
    end_ms: int,
    client: httpx.Client,
) -> list[dict[str, float]]:
    rows: list[list[Any]] = []
    cursor = start_ms
    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": "15m",
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        }
        last_error: Exception | None = None
        payload: Any = None
        for base in PUBLIC_BASE_URLS:
            try:
                response = client.get(f"{base}/api/v3/klines", params=params)
                if response.status_code in {403, 451} or response.status_code >= 500:
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise RuntimeError("unexpected_kline_payload")
                break
            except Exception as exc:
                last_error = exc
                continue
        else:
            raise RuntimeError(f"kline_download_failed:{symbol}:{type(last_error).__name__}")

        if not payload:
            break
        batch = [row for row in payload if isinstance(row, list) and len(row) >= 11]
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + INTERVAL_MS
        if next_cursor <= cursor:
            raise RuntimeError("non_advancing_kline_cursor")
        cursor = next_cursor
        if len(batch) < 1000:
            break
        time.sleep(0.03)

    parsed = [_parse_kline(row) for row in rows]
    dedup = {int(row["open_time"]): row for row in parsed}
    return [dedup[key] for key in sorted(dedup)]


def resample_15m(candles: list[dict[str, float]], factor: int) -> list[dict[str, float]]:
    if factor <= 0:
        raise ValueError("factor must be > 0")
    grouped: dict[int, list[dict[str, float]]] = defaultdict(list)
    bucket_ms = INTERVAL_MS * factor
    for candle in candles:
        open_time = int(candle["open_time"])
        bucket = (open_time // bucket_ms) * bucket_ms
        grouped[bucket].append(candle)

    out: list[dict[str, float]] = []
    for bucket in sorted(grouped):
        group = sorted(grouped[bucket], key=lambda row: row["open_time"])
        if len(group) != factor:
            continue
        expected = [bucket + i * INTERVAL_MS for i in range(factor)]
        if [int(row["open_time"]) for row in group] != expected:
            continue
        out.append(
            {
                "open_time": float(bucket),
                "open": group[0]["open"],
                "high": max(row["high"] for row in group),
                "low": min(row["low"] for row in group),
                "close": group[-1]["close"],
                "volume": sum(row["volume"] for row in group),
                "close_time": float(bucket + bucket_ms - 1),
                "quote_volume": sum(row["quote_volume"] for row in group),
                "trades": sum(row["trades"] for row in group),
                "taker_buy_base": sum(row["taker_buy_base"] for row in group),
                "taker_buy_quote": sum(row["taker_buy_quote"] for row in group),
            }
        )
    return out


def _slice_closed(
    candles: list[dict[str, float]],
    close_times: list[int],
    *,
    at_close_ms: int,
    limit: int = 120,
) -> list[dict[str, float]]:
    end = bisect.bisect_right(close_times, at_close_ms)
    start = max(0, end - limit)
    return candles[start:end]


def _max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _stats(trades: list[ReplayTrade]) -> dict[str, Any]:
    closed = [trade for trade in trades if trade.pnl_usdt is not None]
    pnls = [float(trade.pnl_usdt) for trade in closed]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    gross_profit = sum(wins)
    gross_loss_abs = abs(sum(losses))
    profit_factor = gross_profit / gross_loss_abs if gross_loss_abs > 0 else None
    expectancy = sum(pnls) / len(pnls) if pnls else None
    max_consecutive_losses = 0
    streak = 0
    for pnl in pnls:
        if pnl < 0:
            streak += 1
            max_consecutive_losses = max(max_consecutive_losses, streak)
        else:
            streak = 0
    return {
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "net_pnl_usdt": round(sum(pnls), 8),
        "expectancy_usdt": round(expectancy, 8) if expectancy is not None else None,
        "profit_factor": round(profit_factor, 8) if profit_factor is not None else None,
        "max_drawdown_usdt": round(_max_drawdown(pnls), 8),
        "max_consecutive_losses": max_consecutive_losses,
        "open_at_end": sum(1 for trade in trades if trade.pnl_usdt is None),
        "ambiguous_stop_first": sum(1 for trade in trades if trade.reason == "ambiguous_stop_first"),
    }


def run_replay(
    data: dict[str, list[dict[str, float]]],
    *,
    settings: Settings,
    slippage_bps: float = 5.0,
) -> dict[str, Any]:
    if "BTCUSDT" not in data:
        raise ValueError("BTCUSDT is required for regime alignment")
    if slippage_bps < 0:
        raise ValueError("slippage_bps must be >= 0")

    one_hour = {symbol: resample_15m(rows, 4) for symbol, rows in data.items()}
    four_hour = {symbol: resample_15m(rows, 16) for symbol, rows in data.items()}
    one_hour_close = {
        symbol: [int(row["close_time"]) for row in rows]
        for symbol, rows in one_hour.items()
    }
    four_hour_close = {
        symbol: [int(row["close_time"]) for row in rows]
        for symbol, rows in four_hour.items()
    }
    btc_1h = one_hour["BTCUSDT"]
    btc_1h_close = one_hour_close["BTCUSDT"]

    by_open = {
        symbol: {int(row["open_time"]): (idx, row) for idx, row in enumerate(rows)}
        for symbol, rows in data.items()
    }
    all_times = sorted({timestamp for mapping in by_open.values() for timestamp in mapping})

    trades: list[ReplayTrade] = []
    blocked_until_open_time = -1
    daily_realized: dict[str, float] = defaultdict(float)
    gate_counts: Counter[str] = Counter()
    eligible_signals = 0

    for signal_open_time in all_times:
        if signal_open_time <= blocked_until_open_time:
            continue
        signal_close_ms = signal_open_time + INTERVAL_MS - 1
        candidates: list[tuple[Candidate, int]] = []

        for symbol, rows in data.items():
            current = by_open[symbol].get(signal_open_time)
            if current is None:
                continue
            idx, _ = current
            if idx < 220 * 4:
                continue
            candles_15m = rows[max(0, idx - 119): idx + 1]
            candles_1h = _slice_closed(
                one_hour[symbol], one_hour_close[symbol], at_close_ms=signal_close_ms
            )
            candles_4h = _slice_closed(
                four_hour[symbol], four_hour_close[symbol], at_close_ms=signal_close_ms
            )
            btc_window = _slice_closed(
                btc_1h, btc_1h_close, at_close_ms=signal_close_ms
            )
            if min(len(candles_15m), len(candles_1h), len(candles_4h), len(btc_window)) < 55:
                continue
            quote_volume_24h = sum(row["quote_volume"] for row in rows[max(0, idx - 95): idx + 1])
            try:
                candidate = evaluate_candidate(
                    symbol=symbol,
                    candles_15m=candles_15m,
                    candles_1h=candles_1h,
                    candles_4h=candles_4h,
                    btc_1h=btc_window,
                    # Historical L1 spread is not in kline archives. This replay
                    # explicitly assumes the live spread gate passes and is
                    # therefore DIAGNOSTIC_ONLY, never Live approval.
                    spread_bps=0.0,
                    quote_volume_24h=quote_volume_24h,
                    min_quote_volume_24h=settings.min_quote_volume_24h,
                    max_spread_bps=settings.max_spread_bps,
                    min_score=settings.min_score,
                )
            except ValueError:
                continue
            if candidate.eligible:
                candidates.append((candidate, idx))
            else:
                if not candidate.btc_regime_ok:
                    gate_counts["btc_regime"] += 1
                if not candidate.trend_15m:
                    gate_counts["trend_15m"] += 1
                if not candidate.trend_1h:
                    gate_counts["trend_1h"] += 1
                if not candidate.trend_4h:
                    gate_counts["trend_4h"] += 1
                if not (candidate.breakout_retest or candidate.pullback):
                    gate_counts["entry_setup"] += 1
                if not candidate.rel_volume_ok:
                    gate_counts["relative_volume"] += 1
                if not candidate.taker_flow_ok:
                    gate_counts["taker_flow"] += 1

        if not candidates:
            continue
        eligible_signals += len(candidates)
        candidates.sort(key=lambda item: (item[0].score, item[0].relative_volume), reverse=True)
        candidate, idx = candidates[0]
        rows = data[candidate.symbol]
        if idx + 1 >= len(rows):
            continue

        day = datetime.fromtimestamp(signal_open_time / 1000, tz=timezone.utc).date().isoformat()
        if daily_realized[day] <= -abs(settings.max_daily_loss_usdt):
            continue

        entry_bar = rows[idx + 1]
        entry = entry_bar["open"] * (1.0 + slippage_bps / 10_000.0)
        quantity = settings.trade_size_usdt / entry
        tp = entry * (1.0 + settings.take_profit_pct)
        sl = entry * (1.0 - settings.stop_loss_pct)
        exit_price: float | None = None
        exit_open_time: int | None = None
        reason = "open_at_end"

        for future in rows[idx + 1:]:
            hit_tp = future["high"] >= tp
            hit_sl = future["low"] <= sl
            if hit_tp and hit_sl:
                exit_price = sl * (1.0 - slippage_bps / 10_000.0)
                exit_open_time = int(future["open_time"])
                reason = "ambiguous_stop_first"
                break
            if hit_sl:
                exit_price = sl * (1.0 - slippage_bps / 10_000.0)
                exit_open_time = int(future["open_time"])
                reason = "stop_loss"
                break
            if hit_tp:
                exit_price = tp * (1.0 - slippage_bps / 10_000.0)
                exit_open_time = int(future["open_time"])
                reason = "take_profit"
                break

        pnl: float | None = None
        if exit_price is not None:
            gross = (exit_price - entry) * quantity
            entry_fee = entry * quantity * settings.paper_fee_rate
            exit_fee = exit_price * quantity * settings.paper_fee_rate
            pnl = gross - entry_fee - exit_fee
            exit_day = datetime.fromtimestamp(exit_open_time / 1000, tz=timezone.utc).date().isoformat()
            daily_realized[exit_day] += pnl
            blocked_until_open_time = int(exit_open_time)
        else:
            blocked_until_open_time = rows[-1]["open_time"]

        trades.append(
            ReplayTrade(
                symbol=candidate.symbol,
                setup=candidate.entry_setup,
                signal_open_time=int(candidate.signal_open_time),
                entry_open_time=int(entry_bar["open_time"]),
                exit_open_time=exit_open_time,
                entry_price=entry,
                exit_price=exit_price,
                reason=reason,
                pnl_usdt=round(pnl, 8) if pnl is not None else None,
            )
        )

    stats = _stats(trades)
    setup_counts = Counter(trade.setup for trade in trades)
    symbol_counts = Counter(trade.symbol for trade in trades)
    return {
        "verdict": "DIAGNOSTIC_ONLY_NOT_LIVE_APPROVAL",
        "assumptions": {
            "historical_spread": "ASSUMED_PASS_NO_HISTORICAL_L1",
            "entry": "next_15m_open_plus_slippage",
            "ambiguous_same_candle": "STOP_FIRST_CONSERVATIVE",
            "fee_rate_each_side": settings.paper_fee_rate,
            "slippage_bps_each_side": slippage_bps,
            "max_open_positions": 1,
            "daily_loss_cap_usdt": settings.max_daily_loss_usdt,
            "historical_universe": "fixed_liquid_symbol_panel_not_dynamic_top12",
        },
        "stats": stats,
        "eligible_signals_before_single_position_filter": eligible_signals,
        "setup_counts": dict(setup_counts),
        "symbol_counts": dict(symbol_counts),
        "gate_failure_counts": dict(gate_counts),
        "trades": [trade.to_dict() for trade in trades],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="V2 conservative historical replay diagnostic")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--output", default="v2_historical_replay.json")
    args = parser.parse_args()
    if args.days < 15 or args.days > 365:
        raise SystemExit("--days must be between 15 and 365")

    symbols = tuple(dict.fromkeys(part.strip().upper() for part in args.symbols.split(",") if part.strip()))
    if "BTCUSDT" not in symbols:
        symbols = ("BTCUSDT", *symbols)

    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=args.days)).timestamp() * 1000)
    data: dict[str, list[dict[str, float]]] = {}
    with httpx.Client(timeout=30.0, headers={"User-Agent": "tst-v2-historical-replay/1.0"}) as client:
        for symbol in symbols:
            candles = fetch_spot_15m(symbol, start_ms=start_ms, end_ms=end_ms, client=client)
            if len(candles) < 1000:
                raise RuntimeError(f"insufficient_history:{symbol}:{len(candles)}")
            data[symbol] = candles

    settings = Settings(mode="shadow")
    report = run_replay(data, settings=settings, slippage_bps=args.slippage_bps)
    report["period"] = {
        "days": args.days,
        "start_utc": datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(),
        "end_utc": datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).isoformat(),
        "symbols": list(symbols),
    }
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print(json.dumps({k: v for k, v in report.items() if k != "trades"}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
