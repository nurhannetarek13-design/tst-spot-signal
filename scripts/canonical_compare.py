#!/usr/bin/env python3
import json
import math
import pathlib
import sys
from datetime import datetime, timezone


def load(path):
    return json.loads(pathlib.Path(path).read_text())


def rel_bps(a, b):
    if a == b:
        return 0.0
    denom = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / denom * 10000.0


def compare_trade(c, e, price_tol_bps):
    checks = {
        "symbol": c.get("symbol") == e.get("symbol"),
        "side": c.get("side") == e.get("side"),
        "signal_time": c.get("signal_time") == e.get("signal_time"),
        "entry_time": c.get("entry_time") == e.get("entry_time"),
        "exit_time": c.get("exit_time") == e.get("exit_time"),
        "entry_reason": c.get("entry_reason") == e.get("entry_reason"),
        "exit_reason": c.get("exit_reason") == e.get("exit_reason"),
        "candidate_fingerprint": c.get("candidate_fingerprint") == e.get("candidate_fingerprint"),
    }
    entry_bps = rel_bps(float(c.get("entry_price", 0.0)), float(e.get("entry_price", 0.0)))
    exit_bps = rel_bps(float(c.get("exit_price", 0.0)), float(e.get("exit_price", 0.0)))
    checks["entry_price"] = entry_bps <= price_tol_bps
    checks["exit_price"] = exit_bps <= price_tol_bps
    return checks, entry_bps, exit_bps


def main():
    if len(sys.argv) < 4:
        raise SystemExit("usage: canonical_compare.py CANONICAL_JSON OUT_JSON ENGINE_JSON [ENGINE_JSON ...]")

    canonical = load(sys.argv[1])
    out_path = pathlib.Path(sys.argv[2])
    engine_paths = sys.argv[3:]
    spec = canonical.get("executionSpec") or {}
    price_tol_bps = float(spec.get("priceToleranceBps", 1.0))
    c_trades = canonical.get("trades") or []

    engines = []
    overall_pass = True
    for ep in engine_paths:
        doc = load(ep)
        name = doc.get("engine") or pathlib.Path(ep).stem.upper()
        e_trades = doc.get("trades") or []
        fingerprint_match = doc.get("candidateFingerprint") == canonical.get("candidateFingerprint")
        count_match = len(e_trades) == len(c_trades)
        diffs = []
        first_divergence = None

        for i in range(max(len(c_trades), len(e_trades))):
            if i >= len(c_trades):
                d = {"index": i, "type": "EXTRA_ENGINE_TRADE", "engine": e_trades[i]}
                diffs.append(d)
                first_divergence = first_divergence or d
                continue
            if i >= len(e_trades):
                d = {"index": i, "type": "MISSING_ENGINE_TRADE", "canonical": c_trades[i]}
                diffs.append(d)
                first_divergence = first_divergence or d
                continue
            checks, entry_bps, exit_bps = compare_trade(c_trades[i], e_trades[i], price_tol_bps)
            failed = [k for k, v in checks.items() if not v]
            if failed:
                d = {
                    "index": i,
                    "type": "TRADE_DIVERGENCE",
                    "failed": failed,
                    "entryPriceDeltaBps": entry_bps,
                    "exitPriceDeltaBps": exit_bps,
                    "canonical": c_trades[i],
                    "engine": e_trades[i],
                }
                diffs.append(d)
                first_divergence = first_divergence or d

        signal_gate = fingerprint_match and count_match and not any(
            d.get("type") != "TRADE_DIVERGENCE" or any(x in d.get("failed", []) for x in ["symbol", "side", "signal_time", "entry_reason", "candidate_fingerprint"])
            for d in diffs
        )
        execution_gate = signal_gate and not any(
            any(x in d.get("failed", []) for x in ["entry_time", "exit_time", "exit_reason"])
            for d in diffs if d.get("type") == "TRADE_DIVERGENCE"
        )
        price_gate = execution_gate and not any(
            any(x in d.get("failed", []) for x in ["entry_price", "exit_price"])
            for d in diffs if d.get("type") == "TRADE_DIVERGENCE"
        )
        trade_gate = price_gate and count_match and len(diffs) == 0
        passed = trade_gate
        overall_pass = overall_pass and passed
        engines.append({
            "engine": name,
            "pass": passed,
            "candidateFingerprintMatch": fingerprint_match,
            "canonicalTrades": len(c_trades),
            "engineTrades": len(e_trades),
            "gates": {
                "signalGate": signal_gate,
                "executionGate": execution_gate,
                "priceGate": price_gate,
                "tradeGate": trade_gate,
            },
            "firstDivergence": first_divergence,
            "divergenceCount": len(diffs),
            "divergences": diffs[:50],
        })

    out = {
        "schemaVersion": 1,
        "authority": "CANONICAL_COMPARATOR",
        "candidateId": canonical.get("candidateId"),
        "candidateFingerprint": canonical.get("candidateFingerprint"),
        "status": "PASS" if overall_pass else "FAIL",
        "pass": overall_pass,
        "liveReady": False,
        "smallLiveReviewReady": False,
        "metricGateEvaluated": False,
        "metricGateReason": "Metrics are intentionally deferred until signal/execution/price/trade identity passes.",
        "engines": engines,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    raise SystemExit(0 if overall_pass else 2)


if __name__ == "__main__":
    main()
