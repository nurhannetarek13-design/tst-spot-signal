#!/usr/bin/env python3
"""Multi-year replication for the frozen extreme long-liquidation continuation edge.

Uses exactly the same frozen definition as the discovery candidate, without
re-estimating any threshold:
  liquidation total >= 93,166,010.59829867 USD
  liquidation imbalance <= -0.50
  Binance Vision OI change < 0 on shock day
  Binance Vision taker-buy share < 0.50 on shock day
  direction = SHORT from shock-day close

Replication window: 2022-09-06 through 2025-09-05, segmented by year.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import statistics
from collections import defaultdict

from coinalyze_liquidation_history import API_KEY_ENV, api_get, discover_binance_code, discover_symbols, unix_seconds
from binance_vision_extreme_shock_validation import day_state

AUTHORIZATION = "RESEARCH_ONLY"
FIXED_LIQ_THRESHOLD = 93166010.59829867
FIXED_IMBALANCE_MAX = -0.50
START = dt.datetime(2022, 9, 6, tzinfo=dt.timezone.utc)
END = dt.datetime(2025, 9, 6, tzinfo=dt.timezone.utc)  # exclusive
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
OUT = pathlib.Path(os.getenv("FROZEN_MULTIYEAR_DIR", "/data/frozen-extreme-shock-multiyear"))


def summarize(xs):
    vals = [float(x) for x in xs if x is not None and math.isfinite(float(x))]
    if not vals:
        return {"n": 0, "mean": None, "median": None, "hitRate": None}
    return {
        "n": len(vals),
        "mean": sum(vals) / len(vals),
        "median": statistics.median(vals),
        "hitRate": sum(1 for x in vals if x > 0) / len(vals),
    }


def fetch_liq(api_key, smap):
    payload = api_get("liquidation-history", api_key, {
        "symbols": ",".join(smap.values()),
        "interval": "daily",
        "from": unix_seconds(START),
        "to": unix_seconds(END - dt.timedelta(seconds=1)),
        "convert_to_usd": "true",
    })
    reverse = {v: k for k, v in smap.items()}
    out = []
    for block in payload:
        sym = reverse.get(str(block.get("symbol") or ""), str(block.get("symbol") or ""))
        for p in block.get("history") or []:
            t = int(p.get("t") or 0)
            if not t:
                continue
            d = dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).date()
            if not (START.date() <= d < END.date()):
                continue
            l = float(p.get("l") or 0.0)
            s = float(p.get("s") or 0.0)
            total = l + s
            imb = ((s - l) / total) if total > 0 else 0.0
            out.append({"symbol": sym, "day": d.isoformat(), "long_liq": l, "short_liq": s, "liq_total": total, "liq_imbalance": imb})
    return out


def bucket_year(day: str) -> str:
    d = dt.date.fromisoformat(day)
    # Three fixed 12-month windows aligned to the discovery/OOS boundary.
    if dt.date(2022,9,6) <= d < dt.date(2023,9,6):
        return "2022-09-06_to_2023-09-05"
    if dt.date(2023,9,6) <= d < dt.date(2024,9,6):
        return "2023-09-06_to_2024-09-05"
    if dt.date(2024,9,6) <= d < dt.date(2025,9,6):
        return "2024-09-06_to_2025-09-05"
    return "other"


def main():
    api_key = os.getenv(API_KEY_ENV, "").strip()
    if not api_key:
        raise SystemExit(f"Missing {API_KEY_ENV}")
    code = discover_binance_code(api_key)
    smap = discover_symbols(api_key, SYMBOLS, code)
    raw = fetch_liq(api_key, smap)
    pre = [r for r in raw if r["liq_total"] >= FIXED_LIQ_THRESHOLD and r["liq_imbalance"] <= FIXED_IMBALANCE_MAX]

    cache = {}
    confirmed = []
    failures = []
    for r in pre:
        d0 = dt.date.fromisoformat(r["day"])
        try:
            states = [day_state(r["symbol"], (d0 + dt.timedelta(days=h)).isoformat(), cache) for h in range(4)]
        except Exception as exc:
            failures.append({"symbol":r["symbol"],"day":r["day"],"error":f"{type(exc).__name__}: {exc}"})
            continue
        cur = states[0]
        if not cur:
            failures.append({"symbol":r["symbol"],"day":r["day"],"error":"missing shock-day Binance Vision state"})
            continue
        if cur.get("oi_change") is None or cur["oi_change"] >= 0:
            continue
        if cur.get("taker_buy_share") is None or cur["taker_buy_share"] >= 0.50:
            continue
        x = {**r, "yearBucket": bucket_year(r["day"]), "oi_change":cur["oi_change"], "taker_buy_share":cur["taker_buy_share"]}
        for h in (1,2,3):
            st = states[h]
            long_ret = (st["close"] / cur["close"] - 1.0) if st and cur["close"] > 0 else None
            x[f"short_fwd_{h}d"] = (-long_ret) if long_ret is not None else None
        confirmed.append(x)

    by_year = defaultdict(list)
    for r in confirmed:
        by_year[r["yearBucket"]].append(r)
    yearly = {}
    for y in ("2022-09-06_to_2023-09-05","2023-09-06_to_2024-09-05","2024-09-06_to_2025-09-05"):
        arr = by_year[y]
        yearly[y] = {
            "events": len(arr),
            "shortFwd1d": summarize(r.get("short_fwd_1d") for r in arr),
            "shortFwd2d": summarize(r.get("short_fwd_2d") for r in arr),
            "shortFwd3d": summarize(r.get("short_fwd_3d") for r in arr),
        }

    pooled1 = summarize(r.get("short_fwd_1d") for r in confirmed)
    pooled2 = summarize(r.get("short_fwd_2d") for r in confirmed)
    pooled3 = summarize(r.get("short_fwd_3d") for r in confirmed)
    gate = {
        "meanGrossAtLeast1_5pct": pooled1["mean"] is not None and pooled1["mean"] >= 0.015,
        "medianPositive": pooled1["median"] is not None and pooled1["median"] > 0,
        "hitRateAbove55pct": pooled1["hitRate"] is not None and pooled1["hitRate"] > 0.55,
    }
    gate["pass"] = all(gate.values())

    OUT.mkdir(parents=True, exist_ok=True)
    events_path = OUT / "frozen_multiyear_events.json"
    events_path.write_text(json.dumps(confirmed, indent=2, sort_keys=True), encoding="utf-8")
    report = {
        "authorization":AUTHORIZATION,
        "liveTrading":False,
        "definitionFrozen":True,
        "window":{"from":START.date().isoformat(),"toExclusive":END.date().isoformat()},
        "fixedDefinition":{"liqTotalMinUsd":FIXED_LIQ_THRESHOLD,"liqImbalanceMax":FIXED_IMBALANCE_MAX,"oiChange":"< 0","takerBuyShare":"< 0.50","direction":"SHORT","signalTime":"shock-day close"},
        "coinalyzeRows":len(raw),
        "preConfirmationEvents":len(pre),
        "confirmedEvents":len(confirmed),
        "downloadFailures":failures,
        "yearly":yearly,
        "pooled":{"shortFwd1d":pooled1,"shortFwd2d":pooled2,"shortFwd3d":pooled3},
        "gate":gate,
        "decisionRule":"Reject if the frozen definition does not replicate; do not tune thresholds on this OOS window.",
    }
    report_path = OUT / "frozen_multiyear_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"kind":"frozen_extreme_shock_multiyear_complete","events":str(events_path),"report":str(report_path),**report}, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
