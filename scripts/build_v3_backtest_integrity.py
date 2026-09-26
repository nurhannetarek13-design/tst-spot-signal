#!/usr/bin/env python3
"""Derive V3 backtest-integrity status from current machine evidence.

The methodology input supplies static research-process assertions. Dynamic
claims that can be proven by repository artifacts are always overwritten from
those artifacts and fail closed when evidence is missing.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT=pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from research.backtest_integrity_gate import evaluate


def load_json(path):
    p=pathlib.Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def build(methodology=None, universe=None, replay=None):
    methodology=dict(methodology or {})
    universe=universe or {}
    replay=replay or {}

    derived=dict(methodology)

    universe_ok=bool(
        universe.get("pointInTimeUniverse") is True
        and int(universe.get("historicalSymbolCount") or 0) >= 20
    )
    delisted_ok=bool(
        universe.get("delistedCoverage") is True
        and int(universe.get("delistedSymbolCount") or 0) >= 1
    )
    replay_ok=bool(
        replay.get("replayIntegrityPass") is True
        and replay.get("executionReplayPass") is True
    )

    derived["pointInTimeUniverse"]=universe_ok
    derived["delistedCoverage"]=delisted_ok
    derived["historicalSymbolCount"]=int(universe.get("historicalSymbolCount") or 0)
    derived["delistedSymbolCount"]=int(universe.get("delistedSymbolCount") or 0)

    # These execution claims must come from the Spot replay artifact itself,
    # not from a manually edited methodology JSON.
    derived["latencyIncluded"]=bool(replay_ok and replay.get("latencyIncluded") is True)
    derived["partialFillsIncluded"]=bool(replay_ok and replay.get("partialFillsIncluded") is True)

    # Spread/slippage must be asserted by both the historical methodology and
    # the reconstructed Spot execution evidence.
    derived["spreadIncluded"]=bool(
        methodology.get("spreadIncluded") is True
        and replay_ok
        and replay.get("spreadIncluded") is True
    )
    derived["slippageIncluded"]=bool(
        methodology.get("slippageIncluded") is True
        and replay_ok
        and replay.get("slippageIncluded") is True
    )

    replay_cost=float(replay.get("roundTripCostBps") or 0.0)
    method_cost=float(methodology.get("roundTripCostBps") or 0.0)
    derived["roundTripCostBps"]=max(replay_cost,method_cost)

    derived["evidenceSources"]={
        "pointInTimeUniverse":{
            "engine":universe.get("engine"),
            "historicalSymbolCount":int(universe.get("historicalSymbolCount") or 0),
            "delistedSymbolCount":int(universe.get("delistedSymbolCount") or 0),
        },
        "spotExecutionReplay":{
            "engine":replay.get("engine"),
            "replayIntegrityPass":replay.get("replayIntegrityPass") is True,
            "executionReplayPass":replay.get("executionReplayPass") is True,
            "executionProbeCount":int(replay.get("executionProbeCount") or 0),
            "filledExecutionProbeCount":int(replay.get("filledExecutionProbeCount") or 0),
            "latencyIncluded":replay.get("latencyIncluded") is True,
            "partialFillsIncluded":replay.get("partialFillsIncluded") is True,
            "roundTripCostBps":replay_cost,
        },
    }

    result=evaluate(derived)
    result["derivedInput"]=derived
    return result


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--methodology",default="validation/indicator-v3/backtest-integrity-input.json")
    ap.add_argument("--universe",default="validation/indicator-v3/point-in-time-universe.json")
    ap.add_argument("--replay",default="validation/indicator-v3/spot-v3-replay-aggregate.json")
    ap.add_argument("--output",default="validation/indicator-v3/backtest-integrity-latest.json")
    args=ap.parse_args()

    out=build(
        load_json(args.methodology),
        load_json(args.universe),
        load_json(args.replay),
    )
    p=pathlib.Path(args.output)
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(out,indent=2,sort_keys=True))
    print(json.dumps(out,indent=2,sort_keys=True))


if __name__=="__main__":
    main()
