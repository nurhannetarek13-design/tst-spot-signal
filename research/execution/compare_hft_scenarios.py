#!/usr/bin/env python3
"""Fail-closed comparator for hftbacktest execution scenarios.

Consumes a JSON array of scenario summaries produced by the actual hftbacktest
runner. This file does NOT invent fills. It only decides whether execution
robustness is good enough to advance to paper trading.
"""
import argparse, json, math, sys

REQUIRED_FEEDS = {"L2_ONLY", "L2_PLUS_BOOKTICKER"}
REQUIRED_QUEUES = {"PROB_QUEUE_N1", "PROB_QUEUE_N2", "PROB_QUEUE_N3", "RISK_AVERSE"}
REQUIRED_LAT = {1, 2, 3}


def finite(x):
    return isinstance(x, (int, float)) and math.isfinite(x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    args = ap.parse_args()
    rows = json.load(open(args.input))
    errors = []
    if not isinstance(rows, list) or not rows:
        errors.append("NO_SCENARIOS")
        rows = []

    feeds = {r.get("feed") for r in rows}
    queues = {r.get("queueModel") for r in rows}
    lats = {r.get("latencyMultiplier") for r in rows}
    if not REQUIRED_FEEDS.issubset(feeds): errors.append("MISSING_FEED_VARIANTS")
    if not REQUIRED_QUEUES.issubset(queues): errors.append("MISSING_QUEUE_STRESS")
    if not REQUIRED_LAT.issubset(lats): errors.append("MISSING_LATENCY_STRESS")

    for i, r in enumerate(rows):
        for k in ("fillRate", "netExpectancy", "profitFactor", "slippageBps"):
            if not finite(r.get(k)): errors.append(f"INVALID_{k}_{i}")
        if finite(r.get("netExpectancy")) and r["netExpectancy"] <= 0:
            errors.append(f"NON_POSITIVE_EXPECTANCY_{i}")

    # Feed sensitivity: compare like-for-like scenarios across L2 vs fused TOB.
    idx = {}
    for r in rows:
        key = (r.get("symbol"), r.get("queueModel"), r.get("latencyMultiplier"))
        idx.setdefault(key, {})[r.get("feed")] = r
    feed_sensitive = []
    for key, pair in idx.items():
        if REQUIRED_FEEDS.issubset(pair):
            a, b = pair["L2_ONLY"], pair["L2_PLUS_BOOKTICKER"]
            # Conservative pre-registered sanity thresholds; change only in a new gate version.
            if abs(a["fillRate"] - b["fillRate"]) > 0.10:
                feed_sensitive.append({"key": key, "metric": "fillRate"})
            denom = max(abs(a["netExpectancy"]), 1e-9)
            if abs(a["netExpectancy"] - b["netExpectancy"]) / denom > 0.25:
                feed_sensitive.append({"key": key, "metric": "netExpectancy"})
    if feed_sensitive: errors.append("FEED_SENSITIVE")

    out = {
        "engine": "HFTBACKTEST_EXECUTION_GATE_V1",
        "status": "PAPER_ELIGIBLE" if not errors else "REJECT_EXECUTION_ROBUSTNESS",
        "pass": not errors,
        "errors": sorted(set(errors)),
        "feedSensitivity": feed_sensitive,
        "scenarioCount": len(rows),
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "executorAllowed": False,
        "note": "Passing this gate authorizes paper evaluation only; it never authorizes live trading."
    }
    json.dump(out, open(args.output, "w"), indent=2)
    print(json.dumps(out, indent=2))
    sys.exit(0 if not errors else 2)

if __name__ == "__main__":
    main()
