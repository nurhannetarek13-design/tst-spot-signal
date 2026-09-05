#!/usr/bin/env python3
import csv
import hashlib
import json
import pathlib
import sys
from datetime import datetime, timezone


def load_json(path):
    return json.loads(pathlib.Path(path).read_text())


def load_candles(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({
                "timestamp": r["timestamp"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            })
    return rows


def stable_fingerprint(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:20]


def apply_friction(price, side, fee, slippage, entry=True):
    if side != "LONG":
        raise RuntimeError("canonical adapter currently supports LONG spot only")
    slip = slippage if entry else -slippage
    gross = price * (1 + slip)
    return gross * (1 + fee) if entry else gross * (1 - fee)


def main():
    if len(sys.argv) != 6:
        raise SystemExit("usage: canonical_execution_adapter.py MANIFEST SIGNALS_JSON CANDLES_CSV SPEC_JSON OUT_JSON")

    manifest = load_json(sys.argv[1])
    signal_doc = load_json(sys.argv[2])
    candles = load_candles(sys.argv[3])
    spec = load_json(sys.argv[4])
    out_path = pathlib.Path(sys.argv[5])

    signals = signal_doc.get("signals", signal_doc if isinstance(signal_doc, list) else [])
    candle_index = {c["timestamp"]: i for i, c in enumerate(candles)}
    fee = float(spec.get("feeRate", 0.0))
    slippage = float(spec.get("slippageRate", 0.0))
    cooldown = int(spec.get("cooldownBars", 0))
    same_bar_policy = spec.get("sameBarTpSlPolicy", "STOP_FIRST")

    params = manifest.get("params") or {}
    tp_pct = float(params.get("tp", 0.0) or 0.0)
    sl_pct = float(params.get("sl", 0.0) or 0.0)
    hold_bars = int(params.get("holdBars", signal_doc.get("holdBars", 10**9)))

    trades = []
    last_exit_idx = -10**9

    ordered = sorted(signals, key=lambda x: x["signal_time"])
    for sig in ordered:
        if sig.get("side", "LONG") != "LONG":
            continue
        sidx = candle_index.get(sig["signal_time"])
        if sidx is None or sidx + 1 >= len(candles):
            continue
        entry_idx = sidx + 1
        if entry_idx <= last_exit_idx + cooldown:
            continue

        entry_bar = candles[entry_idx]
        raw_entry = entry_bar["open"]
        entry_price = apply_friction(raw_entry, "LONG", fee, slippage, entry=True)
        tp = raw_entry * (1 + tp_pct) if tp_pct > 0 else None
        sl = raw_entry * (1 - sl_pct) if sl_pct > 0 else None

        exit_idx = min(len(candles) - 1, entry_idx + hold_bars)
        exit_reason = "TIME_EXIT"
        raw_exit = candles[exit_idx]["close"]

        for j in range(entry_idx, exit_idx + 1):
            bar = candles[j]
            hit_tp = tp is not None and bar["high"] >= tp
            hit_sl = sl is not None and bar["low"] <= sl
            if hit_tp and hit_sl:
                if same_bar_policy == "STOP_FIRST":
                    raw_exit, exit_reason, exit_idx = sl, "STOP_LOSS", j
                elif same_bar_policy == "TP_FIRST":
                    raw_exit, exit_reason, exit_idx = tp, "TAKE_PROFIT", j
                else:
                    raise RuntimeError(f"unsupported sameBarTpSlPolicy={same_bar_policy}")
                break
            if hit_sl:
                raw_exit, exit_reason, exit_idx = sl, "STOP_LOSS", j
                break
            if hit_tp:
                raw_exit, exit_reason, exit_idx = tp, "TAKE_PROFIT", j
                break

        exit_price = apply_friction(raw_exit, "LONG", fee, slippage, entry=False)
        reason = sig.get("reason") or sig.get("entry_reason") or "SIGNAL"
        record = {
            "symbol": manifest.get("symbol"),
            "side": "LONG",
            "signal_time": sig["signal_time"],
            "intended_entry_time": entry_bar["timestamp"],
            "entry_time": entry_bar["timestamp"],
            "entry_price": entry_price,
            "exit_time": candles[exit_idx]["timestamp"],
            "exit_price": exit_price,
            "entry_reason": reason,
            "exit_reason": exit_reason,
            "candidate_fingerprint": manifest.get("candidateFingerprint"),
            "pnl_pct_net": (exit_price / entry_price) - 1.0,
        }
        record["trade_fingerprint"] = stable_fingerprint({
            k: record[k] for k in [
                "symbol", "side", "signal_time", "entry_time", "exit_time",
                "entry_reason", "exit_reason", "candidate_fingerprint"
            ]
        })
        trades.append(record)
        last_exit_idx = exit_idx

    out = {
        "schemaVersion": 1,
        "engine": "CANONICAL",
        "authority": True,
        "strategyId": manifest.get("strategyId"),
        "candidateId": manifest.get("candidateId"),
        "candidateFingerprint": manifest.get("candidateFingerprint"),
        "symbol": manifest.get("symbol"),
        "family": manifest.get("family"),
        "timeframe": manifest.get("timeframe"),
        "executionSpec": spec,
        "tradeCount": len(trades),
        "trades": trades,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "liveTrading": False,
        "authorization": "RESEARCH_ONLY"
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(json.dumps({"tradeCount": len(trades), "out": str(out_path)}, indent=2))


if __name__ == "__main__":
    main()
