#!/usr/bin/env python3
"""Frozen out-of-sample test for the extreme long-liquidation continuation edge.

The event definition is frozen from the 2025-09-06..2026-09-06 discovery run:
  liquidation total >= 93,166,010.59829867 USD
  liquidation imbalance <= -0.50 (long liquidations dominate)
  Binance Vision shock-day OI change < 0
  Binance Vision shock-day taker-buy share < 0.50 (sell flow dominates)

No thresholds are re-estimated on OOS. Test window is the immediately preceding
year: 2024-09-06 through 2025-09-05 UTC.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import statistics

from coinalyze_liquidation_history import (
    API_KEY_ENV,
    api_get,
    discover_binance_code,
    discover_symbols,
    unix_seconds,
)
from binance_vision_extreme_shock_validation import day_state

AUTHORIZATION = "RESEARCH_ONLY"
FIXED_LIQ_THRESHOLD = 93166010.59829867
FIXED_IMBALANCE_MAX = -0.50
OOS_START = dt.datetime(2024, 9, 6, tzinfo=dt.timezone.utc)
OOS_END = dt.datetime(2025, 9, 6, tzinfo=dt.timezone.utc)  # exclusive
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
OUT = pathlib.Path(os.getenv("FROZEN_OOS_DIR", "/data/frozen-extreme-shock-oos"))


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
    common = {
        "symbols": ",".join(smap.values()),
        "interval": "daily",
        "from": unix_seconds(OOS_START),
        "to": unix_seconds(OOS_END - dt.timedelta(seconds=1)),
        "convert_to_usd": "true",
    }
    payload = api_get("liquidation-history", api_key, common)
    reverse = {v: k for k, v in smap.items()}
    rows = []
    for block in payload:
        cz = str(block.get("symbol") or "")
        sym = reverse.get(cz, cz)
        for p in block.get("history") or []:
            t = int(p.get("t") or 0)
            if not t:
                continue
            d = dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).date()
            if not (OOS_START.date() <= d < OOS_END.date()):
                continue
            long_liq = float(p.get("l") or 0.0)
            short_liq = float(p.get("s") or 0.0)
            total = long_liq + short_liq
            imb = ((short_liq - long_liq) / total) if total > 0 else 0.0
            rows.append({"symbol": sym, "day": d.isoformat(), "long_liq": long_liq, "short_liq": short_liq, "liq_total": total, "liq_imbalance": imb})
    return rows


def main():
    api_key = os.getenv(API_KEY_ENV, "").strip()
    if not api_key:
        raise SystemExit(f"Missing {API_KEY_ENV}")
    code = discover_binance_code(api_key)
    smap = discover_symbols(api_key, SYMBOLS, code)
    raw = fetch_liq(api_key, smap)

    frozen_liq_events = [
        r for r in raw
        if r["liq_total"] >= FIXED_LIQ_THRESHOLD and r["liq_imbalance"] <= FIXED_IMBALANCE_MAX
    ]

    cache = {}
    confirmed = []
    failures = []
    for r in frozen_liq_events:
        d0 = dt.date.fromisoformat(r["day"])
        try:
            states = [day_state(r["symbol"], (d0 + dt.timedelta(days=h)).isoformat(), cache) for h in range(4)]
        except Exception as exc:
            failures.append({"symbol": r["symbol"], "day": r["day"], "error": f"{type(exc).__name__}: {exc}"})
            continue
        cur = states[0]
        if not cur:
            failures.append({"symbol": r["symbol"], "day": r["day"], "error": "missing shock-day Binance Vision state"})
            continue
        # Frozen confirmation: OI down AND sell flow. No re-tuning.
        if cur.get("oi_change") is None or cur["oi_change"] >= 0:
            continue
        if cur.get("taker_buy_share") is None or cur["taker_buy_share"] >= 0.50:
            continue
        out = {**r, "oi_change": cur["oi_change"], "oi_accel": cur.get("oi_accel"), "taker_buy_share": cur["taker_buy_share"], "shock_close": cur["close"]}
        for h in (1, 2, 3):
            st = states[h]
            long_ret = (st["close"] / cur["close"] - 1.0) if st and cur["close"] > 0 else None
            out[f"long_fwd_{h}d"] = long_ret
            out[f"short_fwd_{h}d"] = (-long_ret) if long_ret is not None else None
        confirmed.append(out)

    short1 = summarize(r.get("short_fwd_1d") for r in confirmed)
    short2 = summarize(r.get("short_fwd_2d") for r in confirmed)
    short3 = summarize(r.get("short_fwd_3d") for r in confirmed)
    gate = {
        "meanGrossAtLeast1_5pct": short1["mean"] is not None and short1["mean"] >= 0.015,
        "medianPositive": short1["median"] is not None and short1["median"] > 0,
        "hitRateAbove55pct": short1["hitRate"] is not None and short1["hitRate"] > 0.55,
    }
    gate["pass"] = all(gate.values())

    OUT.mkdir(parents=True, exist_ok=True)
    events_path = OUT / "frozen_oos_events.json"
    events_path.write_text(json.dumps(confirmed, indent=2, sort_keys=True), encoding="utf-8")
    report = {
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "definitionFrozen": True,
        "oosWindow": {"from": OOS_START.date().isoformat(), "toExclusive": OOS_END.date().isoformat()},
        "fixedDefinition": {
            "liqTotalMinUsd": FIXED_LIQ_THRESHOLD,
            "liqImbalanceMax": FIXED_IMBALANCE_MAX,
            "oiChange": "< 0",
            "takerBuyShare": "< 0.50",
            "direction": "SHORT",
            "signalTime": "shock-day close",
        },
        "coinalyzeRows": len(raw),
        "frozenLiquidationEventsBeforeBinanceConfirmation": len(frozen_liq_events),
        "confirmedEvents": len(confirmed),
        "downloadFailures": failures,
        "shortFwd1d": short1,
        "shortFwd2d": short2,
        "shortFwd3d": short3,
        "gate": gate,
        "note": "This OOS test does not authorize live trading. Sample size and replication remain mandatory even if the raw-return gate passes.",
    }
    report_path = OUT / "frozen_oos_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"kind":"frozen_extreme_shock_oos_complete","events":str(events_path),"report":str(report_path), **report}, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
