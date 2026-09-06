#!/usr/bin/env python3
"""
Historical research harness for TST_DERIVATIVES_PRESSURE_V2.
Read-only public Binance data only. No order placement, no futures execution, no leverage.

Goal: reconstruct the current derivatives-pressure event definition on recent history and
measure unoptimized forward Spot returns at 24h/48h/72h.
"""
import datetime as dt
import json
import math
import pathlib
import statistics
import time
import urllib.parse
import urllib.request

SPOT = "https://data-api.binance.vision"
WWW = "https://www.binance.com"
OUT = pathlib.Path("validation/edges/derivatives-pressure-backtest.json")
SYMBOLS = ["PUMPUSDT", "ASTERUSDT", "WLDUSDT", "ENAUSDT", "NEARUSDT", "SUIUSDT"]
PERIOD = "15m"
LOOKBACK_DAYS = 14
BAR_MS = 15 * 60 * 1000
HORIZONS = {"24h": 96, "48h": 192, "72h": 288}


def get(base, path):
    req = urllib.request.Request(
        base + path,
        headers={
            "User-Agent": "Mozilla/5.0 tst-derivatives-pressure-backtest/1.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.binance.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def qs(params):
    return urllib.parse.urlencode(params)


def fetch_series(path, symbol, start_ms, end_ms, limit=500):
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        params = {"symbol": symbol, "period": PERIOD, "limit": limit, "startTime": cursor, "endTime": end_ms}
        batch = get(WWW, path + "?" + qs(params))
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        last = int(batch[-1].get("timestamp") or 0)
        if last <= cursor:
            break
        cursor = last + BAR_MS
        if len(batch) < limit:
            break
        time.sleep(0.05)
    dedup = {}
    for r in rows:
        ts = int(r.get("timestamp") or 0)
        if ts:
            dedup[ts] = r
    return [dedup[k] for k in sorted(dedup)]


def fetch_spot_klines(symbol, start_ms, end_ms):
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        params = {"symbol": symbol, "interval": PERIOD, "limit": 1000, "startTime": cursor, "endTime": end_ms}
        batch = get(SPOT, "/api/v3/klines?" + qs(params))
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        last = int(batch[-1][0])
        if last <= cursor:
            break
        cursor = last + BAR_MS
        if len(batch) < 1000:
            break
        time.sleep(0.05)
    dedup = {int(r[0]): r for r in rows}
    return [dedup[k] for k in sorted(dedup)]


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def median(xs):
    return statistics.median(xs) if xs else None


def summarize(xs):
    if not xs:
        return {"n": 0, "meanPct": None, "medianPct": None, "hitRatePct": None}
    pct = [x * 100 for x in xs]
    return {
        "n": len(xs),
        "meanPct": round(mean(pct), 4),
        "medianPct": round(median(pct), 4),
        "hitRatePct": round(100 * sum(1 for x in xs if x > 0) / len(xs), 2),
    }


def nearest_index(ts_list, ts):
    # Exact alignment is expected for 15m series; dict lookup is used by caller.
    return None


now = dt.datetime.now(dt.timezone.utc)
end_ms = int(now.timestamp() * 1000) - (int(now.timestamp() * 1000) % BAR_MS)
start_ms = end_ms - LOOKBACK_DAYS * 24 * 60 * 60 * 1000

all_events = []
failures = {}
per_symbol = {}

for symbol in SYMBOLS:
    try:
        oi = fetch_series("/futures/data/openInterestHist", symbol, start_ms, end_ms)
        taker = fetch_series("/futures/data/takerlongshortRatio", symbol, start_ms, end_ms)
        spot = fetch_spot_klines(symbol, start_ms, end_ms + 72 * 60 * 60 * 1000)
        oi_map = {int(r["timestamp"]): r for r in oi}
        taker_map = {int(r["timestamp"]): r for r in taker}
        spot_map = {int(r[0]): r for r in spot}
        timestamps = sorted(set(oi_map) & set(taker_map) & set(spot_map))
        events = []
        for i, ts in enumerate(timestamps):
            # 2h OI change from 8 x 15m bars back.
            prev_ts = ts - 8 * BAR_MS
            if prev_ts not in oi_map:
                continue
            oi0 = float(oi_map[prev_ts].get("sumOpenInterestValue") or oi_map[prev_ts].get("sumOpenInterest") or 0)
            oi1 = float(oi_map[ts].get("sumOpenInterestValue") or oi_map[ts].get("sumOpenInterest") or 0)
            if oi0 <= 0:
                continue
            oi_chg = oi1 / oi0 - 1

            ratios = []
            for j in range(4):
                t = ts - j * BAR_MS
                if t in taker_map:
                    ratios.append(float(taker_map[t].get("buySellRatio") or 1))
            if len(ratios) < 4:
                continue
            taker_ratio = sum(ratios) / len(ratios)

            score = 50
            score += 20 if oi_chg >= 0.02 else (10 if oi_chg > 0 else -10)
            score += 20 if taker_ratio >= 1.15 else (10 if taker_ratio >= 1.05 else -10)
            # Historical funding/basis are omitted here rather than fabricated. Require a stricter
            # core trigger so the event is based only on the two fully reconstructed features.
            core_event = score >= 80 and oi_chg > 0 and taker_ratio >= 1.05
            if not core_event:
                continue

            entry = float(spot_map[ts][4])
            ev = {"symbol": symbol, "timestamp": ts, "oiChange2h": oi_chg, "takerBuySellRatio1h": taker_ratio, "coreScore": score}
            valid_any = False
            for name, bars in HORIZONS.items():
                fts = ts + bars * BAR_MS
                if fts in spot_map:
                    px = float(spot_map[fts][4])
                    ev[name] = px / entry - 1
                    valid_any = True
            if valid_any:
                events.append(ev)
                all_events.append(ev)
        per_symbol[symbol] = {"oiRows": len(oi), "takerRows": len(taker), "spotRows": len(spot), "events": len(events)}
    except Exception as e:
        failures[symbol] = str(e)

summary = {}
for h in HORIZONS:
    vals = [e[h] for e in all_events if h in e and math.isfinite(e[h])]
    summary[h] = summarize(vals)

# Freeze-style research gate matching the user's discovery requirements where measurable.
# MFE/MAE is not reconstructed in this first harness, so it is explicitly left unavailable.
gate_reasons = []
for h in ("24h", "48h", "72h"):
    s = summary[h]
    if not s["n"]:
        gate_reasons.append(f"{h.upper()}_NO_EVENTS")
        continue
    if s["meanPct"] is None or s["meanPct"] < 1.5:
        gate_reasons.append(f"{h.upper()}_MEAN_BELOW_1_50")
    if s["medianPct"] is None or s["medianPct"] <= 0:
        gate_reasons.append(f"{h.upper()}_MEDIAN_NOT_POSITIVE")
    if s["hitRatePct"] is None or s["hitRatePct"] <= 55:
        gate_reasons.append(f"{h.upper()}_HIT_RATE_NOT_ABOVE_55")

report = {
    "schemaVersion": 1,
    "strategyId": "TST_DERIVATIVES_PRESSURE_V2_CORE_HISTORICAL",
    "authorization": "RESEARCH_ONLY",
    "liveTrading": False,
    "lookbackDays": LOOKBACK_DAYS,
    "period": PERIOD,
    "symbols": SYMBOLS,
    "historicalFeatureScope": {
        "openInterest": True,
        "takerBuySellRatio": True,
        "funding": False,
        "basis": False,
        "note": "Funding and basis are intentionally excluded from reconstructed historical trigger; no values were inferred."
    },
    "eventDefinition": "coreScore>=80 AND oiChange2h>0 AND takerBuySellRatio1h>=1.05",
    "perSymbol": per_symbol,
    "eventCount": len(all_events),
    "forwardReturns": summary,
    "mfeMaeRatio": None,
    "validation": {"passed": False if gate_reasons else True, "reasons": gate_reasons},
    "failures": failures,
    "generatedAt": now.isoformat(),
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
