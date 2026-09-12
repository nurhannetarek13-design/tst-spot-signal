from __future__ import annotations

import argparse
import bisect
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import fmean, median
from typing import Any, Callable

import httpx

from .config import Settings
from .historical_replay import (
    DEFAULT_SYMBOLS,
    INTERVAL_MS,
    ReplayTrade,
    _stats,
    fetch_spot_15m,
    resample_15m,
)
from .strategy import _confirmed_breakout_retest, _confirmed_pullback, _trend, ema


FAMILY_NAMES = (
    "strict_current",
    "breakout_continuation",
    "volatility_expansion",
    "htf_pullback_reclaim",
)


def _slice_closed(rows, close_times, at_close_ms, limit=120):
    end = bisect.bisect_right(close_times, at_close_ms)
    return rows[max(0, end - limit):end]


def _common_features(
    c15: list[dict[str, float]],
    c1h: list[dict[str, float]],
    c4h: list[dict[str, float]],
    btc1h: list[dict[str, float]],
    quote_volume_24h: float,
    settings: Settings,
) -> dict[str, Any]:
    last = c15[-1]
    prior20 = c15[-21:-1]
    avg_qv = fmean(row["quote_volume"] for row in prior20)
    relvol = last["quote_volume"] / avg_qv if avg_qv > 0 else 0.0
    taker = last["taker_buy_quote"] / last["quote_volume"] if last["quote_volume"] > 0 else 0.0
    prior_high = max(row["high"] for row in prior20)
    closes = [row["close"] for row in c15]
    ema20 = ema(closes, 20)
    ranges = [max(row["high"] - row["low"], 0.0) for row in prior20]
    median_range = median(ranges) if ranges else 0.0
    last_range = max(last["high"] - last["low"], 0.0)
    close_location = (
        (last["close"] - last["low"]) / last_range if last_range > 0 else 0.0
    )
    breakout_retest, _ = _confirmed_breakout_retest(c15)
    pullback = _confirmed_pullback(c15)
    return {
        "last": last,
        "prior_high": prior_high,
        "relvol": relvol,
        "taker": taker,
        "ema20": ema20,
        "range_ratio": last_range / median_range if median_range > 0 else 0.0,
        "close_location": close_location,
        "liquidity": quote_volume_24h >= settings.min_quote_volume_24h,
        "btc": _trend(btc1h),
        "t15": _trend(c15),
        "t1h": _trend(c1h),
        "t4h": _trend(c4h),
        "breakout_retest": breakout_retest,
        "pullback": pullback,
    }


def family_signal(name: str, f: dict[str, Any]) -> tuple[bool, str]:
    last = f["last"]
    quality = f["liquidity"] and f["btc"] and f["relvol"] >= 1.5 and f["taker"] >= 0.56
    if name == "strict_current":
        ok = quality and f["t15"] and f["t1h"] and f["t4h"] and (
            f["breakout_retest"] or f["pullback"]
        )
        setup = "breakout_retest" if f["breakout_retest"] else "pullback"
        return ok, setup if ok else "none"

    if name == "breakout_continuation":
        raw_breakout = last["close"] > f["prior_high"]
        not_extended = last["close"] <= f["prior_high"] * 1.01
        bullish = last["close"] > last["open"]
        ok = quality and f["t1h"] and f["t4h"] and last["close"] > f["ema20"] and raw_breakout and not_extended and bullish
        return ok, "breakout_continuation" if ok else "none"

    if name == "volatility_expansion":
        bullish = last["close"] > last["open"]
        ok = quality and f["t1h"] and f["t4h"] and last["close"] > f["ema20"] and bullish and f["range_ratio"] >= 1.8 and f["close_location"] >= 0.75
        return ok, "volatility_expansion" if ok else "none"

    if name == "htf_pullback_reclaim":
        ok = quality and f["t1h"] and f["t4h"] and f["pullback"]
        return ok, "htf_pullback_reclaim" if ok else "none"

    raise ValueError(f"unknown family {name}")


def _simulate(
    *,
    family: str,
    data: dict[str, list[dict[str, float]]],
    settings: Settings,
    start_ms: int,
    end_ms: int,
    slippage_bps: float,
) -> dict[str, Any]:
    h1 = {s: resample_15m(rows, 4) for s, rows in data.items()}
    h4 = {s: resample_15m(rows, 16) for s, rows in data.items()}
    h1close = {s: [int(r["close_time"]) for r in rows] for s, rows in h1.items()}
    h4close = {s: [int(r["close_time"]) for r in rows] for s, rows in h4.items()}
    btc = h1["BTCUSDT"]
    btcclose = h1close["BTCUSDT"]
    by_open = {s: {int(r["open_time"]): (i, r) for i, r in enumerate(rows)} for s, rows in data.items()}
    all_times = sorted(t for mapping in by_open.values() for t in mapping if start_ms <= t < end_ms)

    blocked_until = -1
    daily_realized = defaultdict(float)
    trades: list[ReplayTrade] = []
    raw_signals = 0

    for signal_time in all_times:
        if signal_time <= blocked_until:
            continue
        signal_close = signal_time + INTERVAL_MS - 1
        candidates: list[tuple[str, int, str, float]] = []
        for symbol, rows in data.items():
            item = by_open[symbol].get(signal_time)
            if item is None:
                continue
            idx, _ = item
            c15 = rows[max(0, idx - 119):idx + 1]
            c1 = _slice_closed(h1[symbol], h1close[symbol], signal_close)
            c4 = _slice_closed(h4[symbol], h4close[symbol], signal_close)
            b1 = _slice_closed(btc, btcclose, signal_close)
            if min(len(c15), len(c1), len(c4), len(b1)) < 55:
                continue
            qv24 = sum(r["quote_volume"] for r in rows[max(0, idx - 95):idx + 1])
            f = _common_features(c15, c1, c4, b1, qv24, settings)
            ok, setup = family_signal(family, f)
            if ok:
                raw_signals += 1
                # fixed deterministic ranking: rel volume then taker flow
                candidates.append((symbol, idx, setup, f["relvol"] * 10 + f["taker"]))
        if not candidates:
            continue
        candidates.sort(key=lambda x: x[3], reverse=True)
        symbol, idx, setup, _ = candidates[0]
        rows = data[symbol]
        if idx + 1 >= len(rows) or int(rows[idx + 1]["open_time"]) >= end_ms:
            continue
        day = datetime.fromtimestamp(signal_time / 1000, tz=timezone.utc).date().isoformat()
        if daily_realized[day] <= -abs(settings.max_daily_loss_usdt):
            continue

        entry_bar = rows[idx + 1]
        entry = entry_bar["open"] * (1 + slippage_bps / 10000)
        qty = settings.trade_size_usdt / entry
        tp = entry * (1 + settings.take_profit_pct)
        sl = entry * (1 - settings.stop_loss_pct)
        exit_price = None
        exit_time = None
        reason = "open_at_segment_end"
        for future in rows[idx + 1:]:
            ft = int(future["open_time"])
            if ft >= end_ms:
                break
            hit_tp = future["high"] >= tp
            hit_sl = future["low"] <= sl
            if hit_tp and hit_sl:
                exit_price = sl * (1 - slippage_bps / 10000)
                exit_time = ft
                reason = "ambiguous_stop_first"
                break
            if hit_sl:
                exit_price = sl * (1 - slippage_bps / 10000)
                exit_time = ft
                reason = "stop_loss"
                break
            if hit_tp:
                exit_price = tp * (1 - slippage_bps / 10000)
                exit_time = ft
                reason = "take_profit"
                break
        pnl = None
        if exit_price is not None:
            gross = (exit_price - entry) * qty
            fees = (entry * qty + exit_price * qty) * settings.paper_fee_rate
            pnl = gross - fees
            exit_day = datetime.fromtimestamp(exit_time / 1000, tz=timezone.utc).date().isoformat()
            daily_realized[exit_day] += pnl
            blocked_until = exit_time
        else:
            blocked_until = end_ms
        trades.append(ReplayTrade(symbol, setup, signal_time, int(entry_bar["open_time"]), exit_time, entry, exit_price, reason, round(pnl, 8) if pnl is not None else None))

    return {
        "stats": _stats(trades),
        "raw_signals": raw_signals,
        "symbol_counts": dict(Counter(t.symbol for t in trades)),
        "setup_counts": dict(Counter(t.setup for t in trades)),
        "trades": [t.to_dict() for t in trades],
    }


def _passes(stats: dict[str, Any], *, min_closed: int) -> tuple[bool, list[str]]:
    blockers = []
    if int(stats.get("closed", 0)) < min_closed:
        blockers.append("sample_insufficient")
    pf = stats.get("profit_factor")
    if pf is None or float(pf) < 1.20:
        blockers.append("profit_factor_below_1_20")
    exp = stats.get("expectancy_usdt")
    if exp is None or float(exp) <= 0:
        blockers.append("expectancy_nonpositive")
    if float(stats.get("max_drawdown_usdt", 999)) > 2.0:
        blockers.append("max_drawdown_above_2_usdt")
    return not blockers, blockers


def run_discovery(data: dict[str, list[dict[str, float]]], *, settings: Settings, slippage_bps: float = 5.0) -> dict[str, Any]:
    common_start = max(int(rows[0]["open_time"]) for rows in data.values())
    common_end = min(int(rows[-1]["open_time"]) + INTERVAL_MS for rows in data.values())
    split = common_start + int((common_end - common_start) * (2 / 3))
    families = {}
    eligible = []
    for family in FAMILY_NAMES:
        discovery = _simulate(family=family, data=data, settings=settings, start_ms=common_start, end_ms=split, slippage_bps=slippage_bps)
        oos = _simulate(family=family, data=data, settings=settings, start_ms=split, end_ms=common_end, slippage_bps=slippage_bps)
        disc_ok, disc_blockers = _passes(discovery["stats"], min_closed=30)
        oos_ok, oos_blockers = _passes(oos["stats"], min_closed=15)
        accepted = disc_ok and oos_ok
        families[family] = {
            "accepted": accepted,
            "discovery": {k: v for k, v in discovery.items() if k != "trades"},
            "oos": {k: v for k, v in oos.items() if k != "trades"},
            "discovery_blockers": disc_blockers,
            "oos_blockers": oos_blockers,
        }
        if accepted:
            eligible.append(family)
    return {
        "verdict": "CANDIDATE_DISCOVERY_DIAGNOSTIC_NOT_LIVE_APPROVAL",
        "split": {
            "start_utc": datetime.fromtimestamp(common_start / 1000, tz=timezone.utc).isoformat(),
            "split_utc": datetime.fromtimestamp(split / 1000, tz=timezone.utc).isoformat(),
            "end_utc": datetime.fromtimestamp(common_end / 1000, tz=timezone.utc).isoformat(),
            "discovery_fraction": 2 / 3,
            "oos_fraction": 1 / 3,
        },
        "acceptance": {
            "discovery_min_closed": 30,
            "oos_min_closed": 15,
            "profit_factor_min": 1.20,
            "expectancy_min_usdt": 0.0,
            "max_drawdown_usdt": 2.0,
        },
        "assumptions": {
            "historical_spread": "ASSUMED_PASS_NO_HISTORICAL_L1",
            "fee_rate_each_side": settings.paper_fee_rate,
            "slippage_bps_each_side": slippage_bps,
            "max_open_positions": 1,
            "daily_loss_cap_usdt": settings.max_daily_loss_usdt,
            "thresholds": "FROZEN_NO_GRID_SEARCH",
        },
        "eligible_families": eligible,
        "families": families,
    }


def main():
    parser = argparse.ArgumentParser(description="Fixed-family V2 candidate discovery with chronological OOS")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--output", default="v2_candidate_discovery.json")
    args = parser.parse_args()
    if args.days < 90 or args.days > 365:
        raise SystemExit("--days must be between 90 and 365")
    symbols = tuple(dict.fromkeys(x.strip().upper() for x in args.symbols.split(",") if x.strip()))
    if "BTCUSDT" not in symbols:
        symbols = ("BTCUSDT", *symbols)
    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=args.days)).timestamp() * 1000)
    data = {}
    with httpx.Client(timeout=30.0, headers={"User-Agent": "tst-v2-candidate-discovery/1.0"}) as client:
        for symbol in symbols:
            candles = fetch_spot_15m(symbol, start_ms=start_ms, end_ms=end_ms, client=client)
            if len(candles) < 5000:
                raise RuntimeError(f"insufficient_history:{symbol}:{len(candles)}")
            data[symbol] = candles
    report = run_discovery(data, settings=Settings(mode="shadow"), slippage_bps=args.slippage_bps)
    report["period_days"] = args.days
    report["symbols"] = list(symbols)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
