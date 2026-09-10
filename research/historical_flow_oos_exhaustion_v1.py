#!/usr/bin/env python3
"""Frozen OOS validation for the historical-flow exhaustion family.

Candidate frozen from discovery:
  high agg_trade_count (symbol-specific q90 threshold calibrated in discovery)
  -> subsequent 4h downside / SHORT-direction return.

Because the production system is Spot-only, a successful result may only become
an avoid-buy/risk-off/exit filter. It does not authorize shorting or Futures.
Events are de-clustered by the full 4h horizon to avoid overlapping outcomes.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import pathlib

import numpy as np
import pandas as pd

AUTHORIZATION = "RESEARCH_ONLY"
HORIZON_BARS = 48
MIN_POOLED_EVENTS = 20
MIN_SYMBOLS_SUPPORT = 3


def exact_binom_greater(wins: int, n: int) -> float:
    if n <= 0:
        return 1.0
    return min(1.0, sum(math.comb(n, k) for k in range(wins, n + 1)) / (2 ** n))


def metrics(x) -> dict:
    a = np.asarray(list(x), dtype=float)
    a = a[np.isfinite(a)]
    if len(a) == 0:
        return {"n":0,"meanShortReturn":None,"medianShortReturn":None,"shortHitRate":None,"signP":1.0}
    wins = int(np.sum(a > 0))
    nonzero = int(np.sum(a != 0))
    p = exact_binom_greater(wins, nonzero)
    return {
        "n": int(len(a)),
        "meanShortReturn": float(np.mean(a)),
        "medianShortReturn": float(np.median(a)),
        "shortHitRate": float(np.mean(a > 0)),
        "signP": p,
    }


def threshold_map(discovery: dict) -> dict[str,float]:
    out = {}
    for s in discovery.get("symbols", []):
        t = (s.get("thresholds") or {}).get("agg_trade_count") or {}
        if "q90" in t:
            out[str(s["symbol"])] = float(t["q90"])
    return out


def declustered_events(d: pd.DataFrame, threshold: float) -> list[dict]:
    d = d.sort_values("ts").reset_index(drop=True).copy()
    count = pd.to_numeric(d["agg_trade_count"], errors="coerce").to_numpy(dtype=float)
    close = pd.to_numeric(d["trade_close"], errors="coerce").to_numpy(dtype=float)
    ts = pd.to_datetime(d["ts"], utc=True)
    events = []
    i = 0
    last_start = len(d) - HORIZON_BARS
    while i < last_start:
        if np.isfinite(count[i]) and count[i] >= threshold and np.isfinite(close[i]) and close[i] > 0:
            j = i + HORIZON_BARS
            if np.isfinite(close[j]) and close[j] > 0:
                long_ret = close[j] / close[i] - 1.0
                events.append({
                    "ts": ts.iloc[i].isoformat(),
                    "tradeCount": float(count[i]),
                    "threshold": threshold,
                    "entryClose": float(close[i]),
                    "futureClose4h": float(close[j]),
                    "longReturn4h": float(long_ret),
                    "shortDirectionReturn4h": float(-long_ret),
                })
                i += HORIZON_BARS
                continue
        i += 1
    return events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discovery", required=True)
    ap.add_argument("--input", default="artifacts/oos/**/*.parquet")
    ap.add_argument("--output", default="validation/edges/historical-flow-oos-exhaustion-v1.json")
    args = ap.parse_args()

    discovery = json.load(open(args.discovery))
    assert discovery["engine"] == "HISTORICAL_FLOW_DISCOVERY_GATE_V1"
    thresholds = threshold_map(discovery)
    files = sorted(glob.glob(args.input, recursive=True))
    if not files:
        raise SystemExit("no OOS parquet files")

    by_symbol = []
    pooled = []
    all_events = {}
    for path in files:
        d = pd.read_parquet(path)
        symbol = str(d["symbol"].iloc[0])
        if symbol not in thresholds:
            continue
        ev = declustered_events(d, thresholds[symbol])
        vals = [e["shortDirectionReturn4h"] for e in ev]
        m = metrics(vals)
        m.update({"symbol":symbol,"frozenThreshold":thresholds[symbol],"events":len(ev)})
        by_symbol.append(m)
        pooled.extend(vals)
        all_events[symbol] = ev

    pooled_m = metrics(pooled)
    supporting = sum(1 for x in by_symbol if x["n"] >= 2 and x["medianShortReturn"] is not None and x["medianShortReturn"] > 0 and x["shortHitRate"] > 0.5)
    gate = bool(
        pooled_m["n"] >= MIN_POOLED_EVENTS
        and pooled_m["meanShortReturn"] is not None and pooled_m["meanShortReturn"] > 0
        and pooled_m["medianShortReturn"] is not None and pooled_m["medianShortReturn"] > 0
        and pooled_m["shortHitRate"] is not None and pooled_m["shortHitRate"] > 0.60
        and pooled_m["signP"] < 0.05
        and supporting >= MIN_SYMBOLS_SUPPORT
    )

    payload = {
        "engine":"HISTORICAL_FLOW_OOS_EXHAUSTION_V1",
        "authorization":AUTHORIZATION,
        "liveTrading":False,
        "futuresTrading":False,
        "automaticPromotion":False,
        "productionUseIfValidated":"SPOT_RISK_OFF_ONLY",
        "candidateFrozenFrom":"HISTORICAL_FLOW_DISCOVERY_GATE_V1",
        "candidate":{"feature":"agg_trade_count","tail":"HIGH","threshold":"symbol-specific q90 frozen from discovery calibration","expectedDirection":"DOWN / SHORT-direction","horizon":"4h","declusterBars":HORIZON_BARS,"declusterMinutes":240},
        "oosRule":"No threshold recalibration on OOS dates.",
        "bySymbol":sorted(by_symbol,key=lambda x:x["symbol"]),
        "pooled":pooled_m,
        "supportingSymbols":supporting,
        "gate":{"minPooledEvents":MIN_POOLED_EVENTS,"shortHitRate":">0.60","meanShortReturn":">0","medianShortReturn":">0","signTestP":"<0.05","minSupportingSymbols":MIN_SYMBOLS_SUPPORT,"pass":gate},
        "events":all_events,
        "decision":"VALIDATED_RISK_OFF_CANDIDATE" if gate else "REJECT_OR_NEEDS_NEW_INDEPENDENT_EVIDENCE",
        "nextStep":"If passed, test unchanged on unseen symbols before any integration. Never convert this Spot-only filter into a short/Futures strategy.",
    }
    out=pathlib.Path(args.output); out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(payload,indent=2,sort_keys=True,allow_nan=False),encoding="utf-8")
    print(json.dumps({"engine":payload["engine"],"pooled":pooled_m,"supportingSymbols":supporting,"pass":gate,"bySymbol":payload["bySymbol"]},separators=(",",":"),allow_nan=False))


if __name__ == "__main__":
    main()
