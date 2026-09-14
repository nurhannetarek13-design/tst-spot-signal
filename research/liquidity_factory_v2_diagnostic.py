#!/usr/bin/env python3
"""Diagnostic companion for Liquidity Factory V2.

Does NOT relax gates or promote candidates. It ranks failed Discovery candidates
and records exactly which fixed gates they missed, so the next research round is
driven by evidence instead of threshold hacking.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from liquidity_factory_v2 import (
    CostModel,
    RiskModel,
    add_features,
    build_btc_regime_frame,
    candidate_library,
    evaluate_candidate,
    fetch_spot_klines,
)


def fail_reasons(a: dict) -> list[str]:
    out = []
    if a["trades"] < 80:
        out.append(f"trades<{80} ({a['trades']})")
    if a["profitFactor"] < 1.15:
        out.append(f"PF<1.15 ({a['profitFactor']:.3f})")
    if a["avgTradePct"] <= 0:
        out.append(f"avg<=0 ({a['avgTradePct']:.4f}%)")
    if a["medianTradePct"] <= -0.05:
        out.append(f"median<=-0.05% ({a['medianTradePct']:.4f}%)")
    if a["maxDrawdownPct"] > 5.0:
        out.append(f"DD>5% ({a['maxDrawdownPct']:.3f}%)")
    if a["positiveSymbols"] < 3:
        out.append(f"positiveSymbols<3 ({a['positiveSymbols']})")
    return out


def distance_to_gate(a: dict) -> float:
    # Lower is closer. This is diagnostic only and is never a promotion score.
    return (
        max(0.0, 80 - a["trades"]) / 80.0
        + max(0.0, 1.15 - a["profitFactor"]) / 1.15
        + max(0.0, -a["avgTradePct"]) * 5.0
        + max(0.0, -0.05 - a["medianTradePct"]) * 2.0
        + max(0.0, a["maxDrawdownPct"] - 5.0) / 5.0
        + max(0.0, 3 - a["positiveSymbols"]) / 3.0
    )


def row(c, result):
    a = result["aggregate"]
    return {
        "id": c.id,
        "candidate": {
            "htfRegime": c.htf_regime,
            "discount": c.discount,
            "sweepLookback": c.sweep_lookback,
            "sweepDepthAtr": c.sweep_depth_atr,
            "chochLookback": c.choch_lookback,
            "participation": c.participation,
        },
        "metrics": a,
        "failReasons": fail_reasons(a),
        "distanceToFixedGate": distance_to_gate(a),
        "perSymbol": result["perSymbol"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "LINKUSDT"])
    ap.add_argument("--start", default="2024-09-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--output", default="artifacts/liquidity-factory-v2/diagnostic.json")
    args = ap.parse_args()

    raw = {}
    for symbol in args.symbols:
        print(f"Loading {symbol}...")
        df, _, _ = fetch_spot_klines(symbol, args.start, args.end)
        raw[symbol] = df
    btc_regime = build_btc_regime_frame(raw["BTCUSDT"])
    frames = {
        s: add_features(df, btc_regime if s != "BTCUSDT" else None, s)
        for s, df in raw.items()
    }

    rows = []
    for idx, c in enumerate(candidate_library(), 1):
        r = evaluate_candidate(c, frames, "discovery", CostModel(), RiskModel())
        rows.append(row(c, r))
        if idx % 24 == 0:
            print(f"Diagnostic {idx}/96")

    closest = sorted(rows, key=lambda x: x["distanceToFixedGate"])[:15]
    enough = [x for x in rows if x["metrics"]["trades"] >= 20]
    top_pf = sorted(enough, key=lambda x: (x["metrics"]["profitFactor"], x["metrics"]["trades"]), reverse=True)[:15]
    top_avg = sorted(enough, key=lambda x: (x["metrics"]["avgTradePct"], x["metrics"]["trades"]), reverse=True)[:15]
    top_count = sorted(rows, key=lambda x: x["metrics"]["trades"], reverse=True)[:15]

    reason_counts = {}
    for x in rows:
        for reason in x["failReasons"]:
            key = reason.split(" (")[0]
            reason_counts[key] = reason_counts.get(key, 0) + 1

    report = {
        "engine": "LIQUIDITY_FACTORY_V2_DIAGNOSTIC",
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "gateChanged": False,
        "oosTouched": False,
        "candidateCount": len(rows),
        "fixedDiscoveryGate": "trades>=80, PF>=1.15, avg>0, median>-0.05%, DD<=5%, >=3 positive symbols",
        "failureReasonCounts": reason_counts,
        "closestToFixedGate": closest,
        "topByProfitFactorMin20Trades": top_pf,
        "topByAvgTradeMin20Trades": top_avg,
        "topByTradeCount": top_count,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({
        "failureReasonCounts": reason_counts,
        "closest": [
            {"id": x["id"], "metrics": x["metrics"], "failReasons": x["failReasons"]}
            for x in closest[:5]
        ],
    }, indent=2))


if __name__ == "__main__":
    main()
