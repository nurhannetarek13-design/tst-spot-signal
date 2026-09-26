"""Fail-closed research integrity checks for production-grade historical evidence.

This does not generate a historical universe itself. It makes survivorship,
look-ahead, cost and fill assumptions explicit and machine-gated so a result
cannot be labelled production-grade without the required evidence.
"""
from __future__ import annotations
import json,pathlib


REQUIRED = {
    "pointInTimeUniverse": True,
    "delistedCoverage": True,
    "chronologicalOOS": True,
    "walkForward": True,
    "unseenSymbolHoldout": True,
    "lookAheadAudit": True,
    "feesIncluded": True,
    "spreadIncluded": True,
    "slippageIncluded": True,
    "latencyIncluded": True,
    "partialFillsIncluded": True,
}


def evaluate(meta:dict)->dict:
    checks={k:bool(meta.get(k)) is want for k,want in REQUIRED.items()}
    reasons=[f"{k}:MISSING_OR_FALSE" for k,ok in checks.items() if not ok]
    historical_symbols=int(meta.get("historicalSymbolCount") or 0)
    delisted_symbols=int(meta.get("delistedSymbolCount") or 0)
    if historical_symbols<20:
        reasons.append("HISTORICAL_UNIVERSE_TOO_SMALL")
    if delisted_symbols<1:
        reasons.append("NO_DELISTED_SYMBOLS")
    if float(meta.get("roundTripCostBps") or 0)<=0:
        reasons.append("COST_MODEL_MISSING")
    return {
        "engine":"BACKTEST_INTEGRITY_GATE_V1",
        "authorization":"RESEARCH_ONLY",
        "liveTrading":False,
        "productionGradeHistoricalEvidence":not reasons,
        "checks":checks,
        "historicalSymbolCount":historical_symbols,
        "delistedSymbolCount":delisted_symbols,
        "reasons":reasons,
        "note":"Historical evidence failing this gate may still be diagnostic, but cannot support promotion."
    }


def main(path="validation/indicator-v3/backtest-integrity-input.json",output="validation/indicator-v3/backtest-integrity-latest.json"):
    p=pathlib.Path(path)
    meta=json.loads(p.read_text()) if p.exists() else {}
    out=evaluate(meta)
    q=pathlib.Path(output);q.parent.mkdir(parents=True,exist_ok=True);q.write_text(json.dumps(out,indent=2))
    print(json.dumps(out,indent=2))


if __name__=="__main__":
    main()
