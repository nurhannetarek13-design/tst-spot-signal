#!/usr/bin/env python3
"""Assemble V3 historical-integrity input from persisted evidence artifacts.

This script only upgrades evidence fields that are directly supported by
machine-readable artifacts. Missing evidence remains false (fail closed).
"""
from __future__ import annotations

import argparse
import json
import pathlib


def load(path):
    p=pathlib.Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def build(base=None, universe=None, replay=None):
    base=dict(base or {})
    universe=universe or {}
    replay=replay or {}

    replay_ok=bool(
        replay.get("replayIntegrityPass") is True
        and replay.get("executionReplayPass") is True
    )
    latency_ok=bool(
        replay_ok
        and replay.get("latencyIncluded") is True
    )
    partial_fill_ok=bool(
        replay_ok
        and replay.get("partialFillAccountingIncluded") is True
    )

    out=dict(base)
    out.update({
        "source":"V3_MACHINE_ASSEMBLED_HISTORICAL_INTEGRITY",
        "pointInTimeUniverse": universe.get("pointInTimeUniverse") is True,
        "delistedCoverage": universe.get("delistedCoverage") is True,
        "historicalSymbolCount": int(universe.get("historicalSymbolCount") or 0),
        "delistedSymbolCount": int(universe.get("delistedSymbolCount") or 0),
        "latencyIncluded": latency_ok,
        "partialFillsIncluded": partial_fill_ok,
        "evidenceProvenance":{
            "pointInTimeUniverseEngine": universe.get("engine"),
            "spotReplayEngine": replay.get("engine"),
            "spotReplayIntegrityPass": replay.get("replayIntegrityPass") is True,
            "spotExecutionReplayPass": replay.get("executionReplayPass") is True,
            "latencyIncluded": latency_ok,
            "partialFillAccountingIncluded": partial_fill_ok,
        },
        "note":"Assembled only from persisted machine-readable V3 evidence. Missing evidence stays false."
    })
    return out


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--base",default="validation/indicator-v3/backtest-integrity-input.json")
    ap.add_argument("--universe",default="validation/indicator-v3/point-in-time-universe.json")
    ap.add_argument("--replay",default="validation/indicator-v3/spot-v3-replay-aggregate.json")
    ap.add_argument("--output",default="validation/indicator-v3/backtest-integrity-input.json")
    a=ap.parse_args()
    out=build(load(a.base),load(a.universe),load(a.replay))
    p=pathlib.Path(a.output)
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(out,indent=2))
    print(json.dumps(out,indent=2))


if __name__=="__main__":
    main()
