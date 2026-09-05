#!/usr/bin/env python3
"""Strict parity gate: compare normalized validator trades to canonical fixture.
Research-only. This does not authorize live trading.
"""
import argparse, json, math, pathlib, sys


def ts(x):
    return None if x is None else str(x).replace("Z", "+00:00")


def close(a, b, tol):
    if a is None or b is None:
        return a == b
    return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=tol)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--fixture", required=True)
    ap.add_argument("--validator", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--price-tol", type=float, default=1e-8)
    args=ap.parse_args()
    fx=json.loads(pathlib.Path(args.fixture).read_text())
    vd=json.loads(pathlib.Path(args.validator).read_text())
    canonical=fx.get("baseTrades") or fx.get("baseTradesFirst50") or []
    actual=(vd.get("parityDiagnostics") or {}).get("normalizedTrades") or (vd.get("parityDiagnostics") or {}).get("freqtradeFirst50") or []
    expected_total=int((fx.get("base") or {}).get("trades", len(canonical)))
    actual_total=int((vd.get("base") or {}).get("trades", len(actual)))
    failures=[]
    if vd.get("candidateFingerprint") != fx.get("candidateFingerprint") and fx.get("candidateFingerprint"):
        failures.append("candidate_fingerprint")
    if ((vd.get("dataset") or {}).get("sha256") != (fx.get("dataset") or {}).get("sha256")):
        failures.append("dataset_sha256")
    if actual_total != expected_total:
        failures.append("trade_count")
    n=min(len(canonical), len(actual))
    first=None
    for i in range(n):
        c,a=canonical[i],actual[i]
        checks={
            "entry_timestamp": ts(c.get("entryTs")) == ts(a.get("entryTs")),
            "exit_timestamp": ts(c.get("exitTs")) == ts(a.get("exitTs")),
            "entry_price": close(c.get("entryPrice"), a.get("entryPrice"), args.price_tol),
            "exit_price": close(c.get("exitPrice"), a.get("exitPrice"), args.price_tol),
            "exit_reason": str(c.get("reason","")).upper() == str(a.get("reason","")).upper(),
        }
        bad=[k for k,v in checks.items() if not v]
        if bad:
            first={"index":i,"failed":bad,"canonical":c,"actual":a}
            failures.extend(bad)
            break
    passed=not failures and len(actual) >= min(expected_total, len(canonical))
    out={"status":"PARITY_PASS" if passed else "PARITY_FAIL","pass":passed,"expectedTrades":expected_total,"actualTrades":actual_total,"comparedTrades":n,"failures":sorted(set(failures)),"firstDivergence":first,"liveTrading":False,"authorization":"RESEARCH_ONLY"}
    pathlib.Path(args.out).write_text(json.dumps(out,indent=2))
    print(json.dumps(out,indent=2))
    return 0 if passed else 2

if __name__ == "__main__":
    sys.exit(main())
