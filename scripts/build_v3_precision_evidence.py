#!/usr/bin/env python3
"""Build fail-closed V3 precision evidence for future live authorization.

This module never enables live trading. It only answers whether the evidence
package honestly supports the configured precision claim.

A claim can pass only when:
- the holdout is explicitly marked untouched,
- realistic costs are included,
- >=100 holdout trades exist,
- holdout win rate is >=99%,
- holdout net PnL is positive,
- historical backtest integrity is production-grade,
- a point-in-time universe includes delisted symbols,
- Binance Spot microstructure/execution replay passes.

Missing evidence always fails closed.
"""
from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime, timezone


DEFAULT_TARGET = {
    "minimumWinRate": 0.99,
    "minimumHoldoutTrades": 100,
    "requirePositiveNet": True,
}


def load_json(path):
    if not path:
        return {}
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def build_evidence(holdout=None, backtest=None, universe=None, replay=None, target=None):
    holdout = holdout or {}
    backtest = backtest or {}
    universe = universe or {}
    replay = replay or {}
    target = {**DEFAULT_TARGET, **(target or {})}

    trades = int(holdout.get("trades") or 0)
    wins = int(holdout.get("wins") or 0)
    losses = int(holdout.get("losses") or max(0, trades - wins))
    win_rate = float(holdout.get("winRate") or (wins / trades if trades else 0.0))
    net = float(holdout.get("netPnlPerUnit") or holdout.get("netPnlUSDT") or 0.0)
    expectancy = float(holdout.get("expectancyPerUnit") or holdout.get("expectancyUSDT") or (net / trades if trades else 0.0))
    pf = float(holdout.get("profitFactor") or 0.0)

    blockers = []
    if holdout.get("untouched") is not True:
        blockers.append("HOLDOUT_NOT_EXPLICITLY_UNTOUCHED")
    if holdout.get("costsIncluded") is not True:
        blockers.append("REALISTIC_COSTS_NOT_CONFIRMED")
    if trades < int(target["minimumHoldoutTrades"]):
        blockers.append("HOLDOUT_SAMPLE_TOO_SMALL")
    if win_rate < float(target["minimumWinRate"]):
        blockers.append("HOLDOUT_WIN_RATE_BELOW_TARGET")
    if target.get("requirePositiveNet", True) and net <= 0:
        blockers.append("HOLDOUT_NET_NOT_POSITIVE")

    if backtest.get("productionGradeHistoricalEvidence") is not True:
        blockers.append("BACKTEST_INTEGRITY_NOT_PROVEN")
    if universe.get("pointInTimeUniverse") is not True:
        blockers.append("POINT_IN_TIME_UNIVERSE_NOT_PROVEN")
    if universe.get("delistedCoverage") is not True:
        blockers.append("DELISTED_COVERAGE_NOT_PROVEN")
    if replay.get("replayIntegrityPass") is not True:
        blockers.append("SPOT_REPLAY_INTEGRITY_NOT_PROVEN")
    if replay.get("executionReplayPass") is not True:
        blockers.append("SPOT_EXECUTION_REPLAY_NOT_PROVEN")

    claim = not blockers
    return {
        "engine": "INDICATOR_V3_PRECISION_EVIDENCE_V1",
        "authorization": "EVIDENCE_ONLY",
        "liveTrading": False,
        "target": target,
        "holdout": {
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "winRate": win_rate,
            "netPnlPerUnit": net,
            "expectancyPerUnit": expectancy,
            "profitFactor": pf,
            "untouched": holdout.get("untouched") is True,
            "costsIncluded": holdout.get("costsIncluded") is True,
        },
        "backtestIntegrity": {
            "productionGradeHistoricalEvidence": backtest.get("productionGradeHistoricalEvidence") is True,
        },
        "universeIntegrity": {
            "pointInTimeUniverse": universe.get("pointInTimeUniverse") is True,
            "delistedCoverage": universe.get("delistedCoverage") is True,
            "historicalSymbolCount": int(universe.get("historicalSymbolCount") or 0),
            "delistedSymbolCount": int(universe.get("delistedSymbolCount") or 0),
        },
        "spotReplay": {
            "replayIntegrityPass": replay.get("replayIntegrityPass") is True,
            "executionReplayPass": replay.get("executionReplayPass") is True,
        },
        "claimSupported": claim,
        "claimBlockedReasons": blockers,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "note": "claimSupported is evidence-only and never enables live trading by itself.",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", default="validation/indicator-v3/holdout-metrics.json")
    ap.add_argument("--backtest", default="validation/indicator-v3/backtest-integrity-latest.json")
    ap.add_argument("--universe", default="validation/indicator-v3/point-in-time-universe.json")
    ap.add_argument("--replay", default="validation/indicator-v3/spot-v3-replay-aggregate.json")
    ap.add_argument("--output", default="validation/indicator-v3/precision-evidence-latest.json")
    a = ap.parse_args()

    out = build_evidence(
        load_json(a.holdout),
        load_json(a.backtest),
        load_json(a.universe),
        load_json(a.replay),
    )
    p = pathlib.Path(a.output)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
