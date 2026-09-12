from __future__ import annotations

import argparse
import bisect
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

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
from .strategy_pool import STRATEGY_SPECS, StrategySpec, evaluate_strategy


BASE_SLIPPAGE_BPS = 5.0
STRESS_SLIPPAGE_BPS = 10.0


def _slice_closed(rows, close_times, at_close_ms, limit=120):
    end = bisect.bisect_right(close_times, at_close_ms)
    return rows[max(0, end - limit):end]


def _prepare(data: dict[str, list[dict[str, float]]]) -> dict[str, Any]:
    h1 = {symbol: resample_15m(rows, 4) for symbol, rows in data.items()}
    h4 = {symbol: resample_15m(rows, 16) for symbol, rows in data.items()}
    return {
        "h1": h1,
        "h4": h4,
        "h1close": {
            symbol: [int(row["close_time"]) for row in rows]
            for symbol, rows in h1.items()
        },
        "h4close": {
            symbol: [int(row["close_time"]) for row in rows]
            for symbol, rows in h4.items()
        },
        "by_open": {
            symbol: {
                int(row["open_time"]): (index, row)
                for index, row in enumerate(rows)
            }
            for symbol, rows in data.items()
        },
    }


def _simulate(
    *,
    spec: StrategySpec,
    data: dict[str, list[dict[str, float]]],
    prepared: dict[str, Any],
    settings: Settings,
    start_ms: int,
    end_ms: int,
    slippage_bps: float,
) -> dict[str, Any]:
    h1 = prepared["h1"]
    h4 = prepared["h4"]
    h1close = prepared["h1close"]
    h4close = prepared["h4close"]
    by_open = prepared["by_open"]
    btc = h1["BTCUSDT"]
    btcclose = h1close["BTCUSDT"]
    all_times = sorted(
        {
            timestamp
            for mapping in by_open.values()
            for timestamp in mapping
            if start_ms <= timestamp < end_ms
        }
    )

    blocked_until = -1
    daily_realized: dict[str, float] = defaultdict(float)
    trades: list[ReplayTrade] = []
    raw_signals = 0
    regime_counts: Counter[str] = Counter()

    for signal_time in all_times:
        if signal_time <= blocked_until:
            continue
        signal_close = signal_time + INTERVAL_MS - 1
        candidates: list[tuple[str, int, int, float, float, str]] = []

        for symbol, rows in data.items():
            item = by_open[symbol].get(signal_time)
            if item is None:
                continue
            idx, _ = item
            candles_15m = rows[max(0, idx - 119):idx + 1]
            candles_1h = _slice_closed(h1[symbol], h1close[symbol], signal_close)
            candles_4h = _slice_closed(h4[symbol], h4close[symbol], signal_close)
            btc_1h = _slice_closed(btc, btcclose, signal_close)
            if min(len(candles_15m), len(candles_1h), len(candles_4h), len(btc_1h)) < 55:
                continue

            quote_volume_24h = sum(
                row["quote_volume"]
                for row in rows[max(0, idx - 95):idx + 1]
            )
            evaluation = evaluate_strategy(
                spec,
                candles_15m=candles_15m,
                candles_1h=candles_1h,
                candles_4h=candles_4h,
                btc_1h=btc_1h,
                # Historical klines do not contain L1 bid/ask. Spread is
                # explicitly assumed pass, so this remains research-only.
                spread_bps=0.0,
                quote_volume_24h=quote_volume_24h,
                settings=settings,
                mode="shadow",
            )
            regime_counts[evaluation.regime] += 1
            if not evaluation.signal_ok:
                continue
            raw_signals += 1
            f = evaluation.features
            candidates.append(
                (
                    symbol,
                    idx,
                    evaluation.score,
                    float(f["relvol"]),
                    float(f["taker"]),
                    evaluation.regime,
                )
            )

        if not candidates:
            continue
        candidates.sort(key=lambda item: (item[2], item[3], item[4]), reverse=True)
        symbol, idx, _score, _relvol, _taker, regime = candidates[0]
        rows = data[symbol]
        if idx + 1 >= len(rows) or int(rows[idx + 1]["open_time"]) >= end_ms:
            continue

        signal_day = datetime.fromtimestamp(signal_time / 1000, tz=timezone.utc).date().isoformat()
        if daily_realized[signal_day] <= -abs(settings.max_daily_loss_usdt):
            continue

        entry_bar = rows[idx + 1]
        entry = float(entry_bar["open"]) * (1.0 + slippage_bps / 10_000.0)
        quantity = settings.trade_size_usdt / entry
        take_profit = entry * (1.0 + spec.take_profit_pct)
        stop_loss = entry * (1.0 - spec.stop_loss_pct)

        exit_price = None
        exit_time = None
        reason = "open_at_segment_end"
        for future in rows[idx + 1:]:
            future_time = int(future["open_time"])
            if future_time >= end_ms:
                break
            hit_tp = float(future["high"]) >= take_profit
            hit_sl = float(future["low"]) <= stop_loss
            if hit_tp and hit_sl:
                exit_price = stop_loss * (1.0 - slippage_bps / 10_000.0)
                exit_time = future_time
                reason = "ambiguous_stop_first"
                break
            if hit_sl:
                exit_price = stop_loss * (1.0 - slippage_bps / 10_000.0)
                exit_time = future_time
                reason = "stop_loss"
                break
            if hit_tp:
                exit_price = take_profit * (1.0 - slippage_bps / 10_000.0)
                exit_time = future_time
                reason = "take_profit"
                break

        pnl = None
        if exit_price is not None:
            gross = (exit_price - entry) * quantity
            fees = (entry * quantity + exit_price * quantity) * settings.paper_fee_rate
            pnl = gross - fees
            exit_day = datetime.fromtimestamp(exit_time / 1000, tz=timezone.utc).date().isoformat()
            daily_realized[exit_day] += pnl
            blocked_until = exit_time
        else:
            blocked_until = end_ms

        trades.append(
            ReplayTrade(
                symbol=symbol,
                setup=f"{spec.strategy_id}:{regime}",
                signal_open_time=signal_time,
                entry_open_time=int(entry_bar["open_time"]),
                exit_open_time=exit_time,
                entry_price=entry,
                exit_price=exit_price,
                reason=reason,
                pnl_usdt=round(pnl, 8) if pnl is not None else None,
            )
        )

    return {
        "stats": _stats(trades),
        "raw_signals": raw_signals,
        "symbol_counts": dict(Counter(trade.symbol for trade in trades)),
        "regime_counts": dict(regime_counts),
        "trades": [trade.to_dict() for trade in trades],
    }


def _gate(stats: dict[str, Any], *, min_closed: int, stress: bool = False) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    if int(stats.get("closed", 0) or 0) < min_closed:
        blockers.append("sample_insufficient")

    pf_raw = stats.get("profit_factor")
    pf = float(pf_raw) if pf_raw is not None else None
    pf_floor = 1.00 if stress else 1.20
    if pf is None or pf < pf_floor:
        blockers.append(f"profit_factor_below_{str(pf_floor).replace('.', '_')}")

    expectancy_raw = stats.get("expectancy_usdt")
    expectancy = float(expectancy_raw) if expectancy_raw is not None else None
    if expectancy is None or expectancy <= 0:
        blockers.append("expectancy_nonpositive")

    if float(stats.get("max_drawdown_usdt", 999.0) or 0.0) > 2.0:
        blockers.append("max_drawdown_above_2_usdt")
    return not blockers, blockers


def run_discovery(
    data: dict[str, list[dict[str, float]]],
    *,
    settings: Settings,
    base_slippage_bps: float = BASE_SLIPPAGE_BPS,
    stress_slippage_bps: float = STRESS_SLIPPAGE_BPS,
) -> dict[str, Any]:
    if "BTCUSDT" not in data:
        raise ValueError("BTCUSDT required")
    prepared = _prepare(data)
    common_start = max(int(rows[0]["open_time"]) for rows in data.values())
    common_end = min(int(rows[-1]["open_time"]) + INTERVAL_MS for rows in data.values())
    split = common_start + int((common_end - common_start) * (2 / 3))

    strategies: dict[str, Any] = {}
    promotable: list[str] = []
    for spec in STRATEGY_SPECS:
        discovery = _simulate(
            spec=spec,
            data=data,
            prepared=prepared,
            settings=settings,
            start_ms=common_start,
            end_ms=split,
            slippage_bps=base_slippage_bps,
        )
        oos = _simulate(
            spec=spec,
            data=data,
            prepared=prepared,
            settings=settings,
            start_ms=split,
            end_ms=common_end,
            slippage_bps=base_slippage_bps,
        )
        stress = _simulate(
            spec=spec,
            data=data,
            prepared=prepared,
            settings=settings,
            start_ms=split,
            end_ms=common_end,
            slippage_bps=stress_slippage_bps,
        )

        discovery_ok, discovery_blockers = _gate(discovery["stats"], min_closed=30)
        oos_ok, oos_blockers = _gate(oos["stats"], min_closed=15)
        stress_ok, stress_blockers = _gate(stress["stats"], min_closed=15, stress=True)
        accepted = discovery_ok and oos_ok and stress_ok
        if accepted:
            promotable.append(spec.strategy_id)

        strategies[spec.strategy_id] = {
            "family": spec.family,
            "accepted": accepted,
            "take_profit_pct": spec.take_profit_pct,
            "stop_loss_pct": spec.stop_loss_pct,
            "discovery": {key: value for key, value in discovery.items() if key != "trades"},
            "oos": {key: value for key, value in oos.items() if key != "trades"},
            "oos_stress": {key: value for key, value in stress.items() if key != "trades"},
            "discovery_blockers": discovery_blockers,
            "oos_blockers": oos_blockers,
            "stress_blockers": stress_blockers,
        }

    return {
        "verdict": "MULTI_STRATEGY_DISCOVERY_DIAGNOSTIC_NOT_LIVE_APPROVAL",
        "promotable_to_paper": promotable,
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
            "base_profit_factor_min": 1.20,
            "stress_profit_factor_min": 1.00,
            "expectancy_min_usdt": 0.0,
            "max_drawdown_usdt": 2.0,
            "base_slippage_bps_each_side": base_slippage_bps,
            "stress_slippage_bps_each_side": stress_slippage_bps,
        },
        "assumptions": {
            "historical_spread": "ASSUMED_PASS_NO_HISTORICAL_L1",
            "fee_rate_each_side": settings.paper_fee_rate,
            "trade_size_usdt": settings.trade_size_usdt,
            "max_open_positions": 1,
            "daily_loss_cap_usdt": settings.max_daily_loss_usdt,
            "thresholds": "FROZEN_NO_POST_HOC_GRID_SEARCH",
        },
        "strategies": strategies,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Frozen V2 multi-strategy Discovery + temporal OOS")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--output", default="v2_multistrategy_discovery.json")
    args = parser.parse_args()
    if args.days < 120 or args.days > 365:
        raise SystemExit("--days must be between 120 and 365")

    symbols = tuple(
        dict.fromkeys(
            value.strip().upper()
            for value in args.symbols.split(",")
            if value.strip()
        )
    )
    if "BTCUSDT" not in symbols:
        symbols = ("BTCUSDT", *symbols)

    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=args.days)).timestamp() * 1000)
    data: dict[str, list[dict[str, float]]] = {}
    with httpx.Client(timeout=30.0, headers={"User-Agent": "tst-v2-multistrategy-discovery/1.0"}) as client:
        for symbol in symbols:
            rows = fetch_spot_15m(symbol, start_ms=start_ms, end_ms=end_ms, client=client)
            if len(rows) < 8_000:
                raise RuntimeError(f"insufficient_history:{symbol}:{len(rows)}")
            data[symbol] = rows

    report = run_discovery(data, settings=Settings(mode="shadow"))
    report["period_days"] = args.days
    report["symbols"] = list(symbols)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
