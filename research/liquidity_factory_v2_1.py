#!/usr/bin/env python3
"""Liquidity Factory V2.1: corrected gate for asymmetric 2R payoff systems.

Candidate definitions and execution are IDENTICAL to V2. Only the Discovery gate
removes the inappropriate positive-median requirement. With a 2R target, a robust
positive-expectancy strategy can legitimately have <50% winners and therefore a
negative median trade. Validation and OOS remain untouched and determine whether
this survives outside Discovery.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
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


def discovery_pass_v21(m: dict) -> bool:
    a = m["aggregate"]
    return (
        a["trades"] >= 80
        and a["profitFactor"] >= 1.20
        and a["avgTradePct"] >= 0.15
        and a["maxDrawdownPct"] <= 5.0
        and a["positiveSymbols"] >= 3
    )


def validation_pass_v21(m: dict) -> bool:
    a = m["aggregate"]
    return (
        a["trades"] >= 25
        and a["profitFactor"] >= 1.10
        and a["avgTradePct"] > 0
        and a["maxDrawdownPct"] <= 4.0
        and a["positiveSymbols"] >= 3
    )


def oos_pass_v21(base: dict, stress: dict) -> bool:
    a = base["aggregate"]
    s = stress["aggregate"]
    return (
        a["trades"] >= 25
        and a["profitFactor"] >= 1.15
        and a["avgTradePct"] > 0
        and a["maxDrawdownPct"] <= 3.0
        and a["positiveSymbols"] >= 3
        and s["profitFactor"] >= 1.00
        and s["avgTradePct"] > 0
    )


def freeze_score(disc: dict, val: dict) -> float:
    da, va = disc["aggregate"], val["aggregate"]
    return (
        min(da["profitFactor"], 4.0)
        + 2.0 * min(va["profitFactor"], 4.0)
        + 5.0 * max(0.0, va["avgTradePct"])
        + 0.15 * va["positiveSymbols"]
        - 0.10 * va["maxDrawdownPct"]
    )


def cdict(c):
    return {**asdict(c), "id": c.id}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "LINKUSDT"])
    ap.add_argument("--start", default="2024-09-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--top-discovery", type=int, default=20)
    ap.add_argument("--top-validation", type=int, default=8)
    ap.add_argument("--output", default="artifacts/liquidity-factory-v2-1/report.json")
    args = ap.parse_args()

    base_cost = CostModel()
    stress_cost = CostModel(fee_rate=0.0015, slippage_rate=0.0010)
    risk = RiskModel()

    raw = {}
    meta = {}
    for s in args.symbols:
        print(f"Loading {s}...")
        df, _, coverage = fetch_spot_klines(s, args.start, args.end)
        raw[s] = df
        meta[s] = {"rows": len(df), "coverage": coverage}
    btc = build_btc_regime_frame(raw["BTCUSDT"])
    frames = {s: add_features(df, btc if s != "BTCUSDT" else None, s) for s, df in raw.items()}

    discovery_rows = []
    for i, c in enumerate(candidate_library(), 1):
        d = evaluate_candidate(c, frames, "discovery", base_cost, risk)
        if discovery_pass_v21(d):
            discovery_rows.append({"candidate": c, "discovery": d})
        if i % 24 == 0:
            print(f"Discovery {i}/96")
    discovery_rows.sort(
        key=lambda x: (
            x["discovery"]["aggregate"]["profitFactor"],
            x["discovery"]["aggregate"]["avgTradePct"],
        ), reverse=True
    )
    shortlist = discovery_rows[:args.top_discovery]

    validation_rows = []
    for x in shortlist:
        c = x["candidate"]
        v = evaluate_candidate(c, frames, "validation", base_cost, risk)
        validation_rows.append({
            "candidate": c,
            "discovery": x["discovery"],
            "validation": v,
            "pass": validation_pass_v21(v),
            "score": freeze_score(x["discovery"], v),
        })
    passed_val = [x for x in validation_rows if x["pass"]]
    passed_val.sort(key=lambda x: x["score"], reverse=True)
    frozen = passed_val[:args.top_validation]

    oos_rows = []
    for x in frozen:
        c = x["candidate"]
        base = evaluate_candidate(c, frames, "oos", base_cost, risk)
        stress = evaluate_candidate(c, frames, "oos", stress_cost, risk)
        oos_rows.append({
            "candidate": c,
            "base": base,
            "stress": stress,
            "pass": oos_pass_v21(base, stress),
        })
    passing = [x for x in oos_rows if x["pass"]]

    report = {
        "engine": "LIQUIDITY_FACTORY_V2_1",
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "candidateDefinitionsChangedFromV2": False,
        "gateMethodologyChange": {
            "changed": True,
            "reason": "Positive-median trade gate is incompatible with intentionally asymmetric 2R payoff systems; PF/expectancy/robustness remain required.",
            "oosWasUntouchedBeforeChange": True,
        },
        "selectionProtocol": {
            "oosUsedForSelection": False,
            "frozenBeforeOos": True,
            "baseCosts": asdict(base_cost),
            "stressCosts": asdict(stress_cost),
            "risk": asdict(risk),
        },
        "window": {"start": args.start, "end": args.end, "timeframe": "15m", "split": [0.60, 0.20, 0.20]},
        "data": meta,
        "gates": {
            "discovery": "trades>=80, PF>=1.20, net avg trade>=0.15%, DD<=5%, >=3 positive symbols",
            "validation": "trades>=25, PF>=1.10, avg>0, DD<=4%, >=3 positive symbols",
            "oos": "trades>=25, PF>=1.15, avg>0, DD<=3%, >=3 positive symbols; stressed PF>=1.0 and avg>0",
        },
        "discovery": {
            "tested": 96,
            "eligible": len(discovery_rows),
            "shortlist": [{"candidate": cdict(x["candidate"]), "metrics": x["discovery"]["aggregate"]} for x in shortlist],
        },
        "validation": {
            "tested": len(validation_rows),
            "passed": len(passed_val),
            "frozenFinalists": [cdict(x["candidate"]) for x in frozen],
            "rows": [{
                "candidate": cdict(x["candidate"]),
                "pass": x["pass"],
                "discovery": x["discovery"]["aggregate"],
                "validation": x["validation"]["aggregate"],
            } for x in validation_rows],
        },
        "oos": {
            "tested": len(oos_rows),
            "passed": len(passing),
            "rows": [{
                "candidate": cdict(x["candidate"]),
                "pass": x["pass"],
                "base": x["base"]["aggregate"],
                "stress": x["stress"]["aggregate"],
                "basePerSymbol": x["base"]["perSymbol"],
            } for x in oos_rows],
        },
        "promotionGate": {
            "decision": "RESEARCH_PASS_NOT_LIVE" if passing else "REJECT",
            "passingCandidates": [cdict(x["candidate"]) for x in passing],
            "canEnableLiveTrading": False,
            "nextStage": "PAPER_REVIEW_ONLY" if passing else "NONE",
        },
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({
        "discoveryEligible": len(discovery_rows),
        "validationPassed": len(passed_val),
        "frozenFinalists": [x["candidate"].id for x in frozen],
        "oosPassed": len(passing),
        "promotionGate": report["promotionGate"],
        "oosRows": report["oos"]["rows"],
    }, indent=2))


if __name__ == "__main__":
    main()
