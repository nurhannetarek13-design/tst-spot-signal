from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import Settings
from .historical_replay import DEFAULT_SYMBOLS, INTERVAL_MS, _stats, fetch_spot_15m
from .multistrategy_discovery import _simulate
from .strategy_pool import STRATEGY_SPECS


def _regime_from_setup(setup: str) -> str:
    parts = str(setup).split(":", 1)
    return parts[1] if len(parts) == 2 else "unknown"


def _group_trades(result: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in result.get("trades", []):
        grouped[_regime_from_setup(trade.get("setup", ""))].append(trade)
    return grouped


def _stats_from_dicts(trades: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(t["pnl_usdt"]) for t in trades if t.get("pnl_usdt") is not None]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "closed": len(values),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(values)) if values else None,
        "net_pnl_usdt": round(sum(values), 8),
        "expectancy_usdt": round(sum(values) / len(values), 8) if values else None,
        "profit_factor": round(gross_profit / gross_loss, 8) if gross_loss > 0 else None,
        "max_drawdown_usdt": round(max_dd, 8),
    }


def _gate(stats: dict[str, Any], *, min_closed: int, pf_min: float, dd_max: float = 1.5) -> tuple[bool, list[str]]:
    blockers = []
    if int(stats.get("closed", 0) or 0) < min_closed:
        blockers.append("sample_insufficient")
    pf = stats.get("profit_factor")
    if pf is None or float(pf) < pf_min:
        blockers.append("profit_factor_below_floor")
    expectancy = stats.get("expectancy_usdt")
    if expectancy is None or float(expectancy) <= 0:
        blockers.append("expectancy_nonpositive")
    if float(stats.get("max_drawdown_usdt", 999.0) or 0.0) > dd_max:
        blockers.append("drawdown_above_floor")
    return not blockers, blockers


def run(data: dict[str, list[dict[str, float]]], *, settings: Settings) -> dict[str, Any]:
    common_start = max(int(rows[0]["open_time"]) for rows in data.values())
    common_end = min(int(rows[-1]["open_time"]) + INTERVAL_MS for rows in data.values())
    split = common_start + int((common_end - common_start) * (2 / 3))
    results = {}
    promotable = []

    for spec in STRATEGY_SPECS:
        discovery = _simulate(
            spec=spec,
            data=data,
            prepared=__import__("v2_bot.multistrategy_discovery", fromlist=["_prepare"])._prepare(data),
            settings=settings,
            start_ms=common_start,
            end_ms=split,
            slippage_bps=5.0,
        )
        oos = _simulate(
            spec=spec,
            data=data,
            prepared=__import__("v2_bot.multistrategy_discovery", fromlist=["_prepare"])._prepare(data),
            settings=settings,
            start_ms=split,
            end_ms=common_end,
            slippage_bps=5.0,
        )
        stress = _simulate(
            spec=spec,
            data=data,
            prepared=__import__("v2_bot.multistrategy_discovery", fromlist=["_prepare"])._prepare(data),
            settings=settings,
            start_ms=split,
            end_ms=common_end,
            slippage_bps=10.0,
        )
        d_groups = _group_trades(discovery)
        o_groups = _group_trades(oos)
        s_groups = _group_trades(stress)
        regime_rows = {}
        accepted_regimes = []

        for regime in spec.allowed_regimes:
            d_stats = _stats_from_dicts(d_groups.get(regime, []))
            o_stats = _stats_from_dicts(o_groups.get(regime, []))
            s_stats = _stats_from_dicts(s_groups.get(regime, []))
            d_ok, d_blockers = _gate(d_stats, min_closed=12, pf_min=1.30)
            o_ok, o_blockers = _gate(o_stats, min_closed=8, pf_min=1.30)
            s_ok, s_blockers = _gate(s_stats, min_closed=8, pf_min=1.05)

            combined = _stats_from_dicts(d_groups.get(regime, []) + o_groups.get(regime, []))
            combined_ok, combined_blockers = _gate(combined, min_closed=20, pf_min=1.25)
            accepted = d_ok and o_ok and s_ok and combined_ok
            if accepted:
                accepted_regimes.append(regime)
                promotable.append({"strategy_id": spec.strategy_id, "regime": regime})
            regime_rows[regime] = {
                "accepted": accepted,
                "discovery": d_stats,
                "oos": o_stats,
                "oos_stress": s_stats,
                "combined_base": combined,
                "blockers": {
                    "discovery": d_blockers,
                    "oos": o_blockers,
                    "oos_stress": s_blockers,
                    "combined": combined_blockers,
                },
            }

        results[spec.strategy_id] = {
            "family": spec.family,
            "accepted_regimes": accepted_regimes,
            "regimes": regime_rows,
        }

    return {
        "verdict": "REGIME_ROUTER_OOS_RESEARCH_ONLY",
        "liveTrading": False,
        "promotable_to_paper": promotable,
        "acceptance": {
            "discovery_min_closed": 12,
            "oos_min_closed": 8,
            "combined_min_closed": 20,
            "discovery_pf_min": 1.30,
            "oos_pf_min": 1.30,
            "stress_pf_min": 1.05,
            "combined_pf_min": 1.25,
            "expectancy": ">0 in every gate",
            "max_drawdown_usdt": 1.5,
            "thresholds": "FROZEN_BEFORE_REGIME_RESULTS_NO_GRID_SEARCH",
        },
        "split": {
            "start_utc": datetime.fromtimestamp(common_start / 1000, tz=timezone.utc).isoformat(),
            "split_utc": datetime.fromtimestamp(split / 1000, tz=timezone.utc).isoformat(),
            "end_utc": datetime.fromtimestamp(common_end / 1000, tz=timezone.utc).isoformat(),
        },
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--output", default="v2_regime_oos.json")
    args = parser.parse_args()
    symbols = tuple(dict.fromkeys(v.strip().upper() for v in args.symbols.split(",") if v.strip()))
    if "BTCUSDT" not in symbols:
        symbols = ("BTCUSDT", *symbols)
    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=args.days)).timestamp() * 1000)
    data = {}
    with httpx.Client(timeout=30.0, headers={"User-Agent": "tst-v2-regime-oos/1.0"}) as client:
        for symbol in symbols:
            rows = fetch_spot_15m(symbol, start_ms=start_ms, end_ms=end_ms, client=client)
            if len(rows) < 8_000:
                raise RuntimeError(f"insufficient_history:{symbol}:{len(rows)}")
            data[symbol] = rows
    report = run(data, settings=Settings(mode="shadow"))
    report["period_days"] = args.days
    report["symbols"] = list(symbols)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
