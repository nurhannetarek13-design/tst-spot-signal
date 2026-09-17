"""Read-only V1/V2 comparison of independently replayed closed trades.

This is a comparison gate, NOT a backtest engine. No exchange credentials,
network, order creation, strategy selection, or production flags are used.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_MANIFEST = (
    "data_hash", "start_utc", "end_utc", "oos_start_utc", "symbols",
    "fee_each_side", "slippage_each_side", "starting_capital_usdt",
    "closed_candle_only", "next_bar_execution", "stop_first", "historical_l2_complete",
    "strategy_commit", "paper_only", "replay_complete",
)
REQUIRED_TRADE = (
    "symbol", "entry_time_utc", "exit_time_utc", "entry_price",
    "exit_price", "quote_size_usdt",
)


def timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"Timestamp missing UTC offset: {value}")
    return dt.astimezone(timezone.utc)


def load_manifest(path: Path) -> dict:
    item = json.loads(path.read_text(encoding="utf-8"))
    missing = [key for key in REQUIRED_MANIFEST if key not in item]
    if missing:
        raise ValueError(f"{path}: missing manifest fields {missing}")
    return item


def comparability(v1: dict, v2: dict) -> list[str]:
    failures: list[str] = []
    for field in ("data_hash", "start_utc", "end_utc", "oos_start_utc", "fee_each_side", "slippage_each_side", "starting_capital_usdt"):
        if v1[field] != v2[field]:
            failures.append(f"MISMATCH:{field}")
    if sorted(v1["symbols"]) != sorted(v2["symbols"]):
        failures.append("MISMATCH:symbols")
    for version, manifest in (("v1", v1), ("v2", v2)):
        for flag in ("closed_candle_only", "next_bar_execution", "stop_first", "historical_l2_complete", "paper_only", "replay_complete"):
            if manifest[flag] is not True:
                failures.append(f"{version}:{flag}:NOT_PROVEN")
        if not manifest["strategy_commit"]:
            failures.append(f"{version}:strategy_commit:MISSING")
        if timestamp(manifest["start_utc"]) >= timestamp(manifest["oos_start_utc"]):
            failures.append(f"{version}:BAD_OOS_SPLIT")
        if timestamp(manifest["oos_start_utc"]) >= timestamp(manifest["end_utc"]):
            failures.append(f"{version}:BAD_END")
    return failures


def trades(path: Path, manifest: dict) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or any(field not in reader.fieldnames for field in REQUIRED_TRADE):
            raise ValueError(f"{path}: missing required trade columns")
        rows = list(reader)
    start, end = timestamp(manifest["start_utc"]), timestamp(manifest["end_utc"])
    normalized = []
    seen = set()
    for row in rows:
        enter, exit_ = timestamp(row["entry_time_utc"]), timestamp(row["exit_time_utc"])
        symbol = row["symbol"]
        if symbol not in manifest["symbols"] or not start <= enter < exit_ <= end:
            raise ValueError(f"{path}: symbol or timestamp outside replay window")
        key = (symbol, enter, exit_)
        if key in seen:
            raise ValueError(f"{path}: duplicate trade {key}")
        seen.add(key)
        entry, exit_price, quote = (float(row[key]) for key in ("entry_price", "exit_price", "quote_size_usdt"))
        if not all(math.isfinite(x) and x > 0 for x in (entry, exit_price, quote)):
            raise ValueError(f"{path}: bad nonpositive/nonfinite price or size")
        fee, slip = float(manifest["fee_each_side"]), float(manifest["slippage_each_side"])
        qty = quote / (entry * (1 + slip))
        entry_cost = qty * entry * (1 + slip) * (1 + fee)
        exit_proceeds = qty * exit_price * (1 - slip) * (1 - fee)
        normalized.append({"exit": exit_, "pnl": exit_proceeds - entry_cost})
    return sorted(normalized, key=lambda t: t["exit"])


def metrics(rows: list[dict]) -> dict:
    wins = [row["pnl"] for row in rows if row["pnl"] > 0]
    losses = [row["pnl"] for row in rows if row["pnl"] < 0]
    equity = peak = drawdown = 0.0
    for row in rows:
        equity += row["pnl"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(rows),
        "win_rate_pct": round(len(wins) / len(rows) * 100, 4) if rows else None,
        "net_pnl_usdt": round(equity, 6),
        "profit_factor": round(sum(wins) / -sum(losses), 6) if losses else None,
        "expectancy_usdt": round(equity / len(rows), 6) if rows else None,
        "max_closed_equity_drawdown_usdt": round(drawdown, 6),
    }


def compare(v1_manifest: Path, v1_csv: Path, v2_manifest: Path, v2_csv: Path) -> dict:
    v1, v2 = load_manifest(v1_manifest), load_manifest(v2_manifest)
    failures = comparability(v1, v2)
    if failures:
        return {"status": "NOT_COMPARABLE", "reasons": failures, "live_trading": False}
    try:
        a, b = trades(v1_csv, v1), trades(v2_csv, v2)
    except (OSError, ValueError) as exc:
        return {"status": "NOT_COMPARABLE", "reasons": [f"INVALID_TRADES:{exc}"], "live_trading": False}
    split = timestamp(v1["oos_start_utc"])
    result = {name: {"full": metrics(rows), "oos": metrics([x for x in rows if x["exit"] >= split])} for name, rows in (("v1", a), ("v2", b))}
    if not a or not b or not result["v1"]["oos"]["trades"] or not result["v2"]["oos"]["trades"]:
        return {"status": "INSUFFICIENT_TRADES", "results": result, "live_trading": False}
    return {"status": "COMPARABLE_DESCRIPTIVE_ONLY", "results": result, "live_trading": False, "note": "No statistical winner or live-trading approval implied."}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    for arg in ("v1_manifest", "v1_trades", "v2_manifest", "v2_trades"):
        p.add_argument(arg, type=Path)
    args = p.parse_args()
    result = compare(args.v1_manifest, args.v1_trades, args.v2_manifest, args.v2_trades)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "COMPARABLE_DESCRIPTIVE_ONLY":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
