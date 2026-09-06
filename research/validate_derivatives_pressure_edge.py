#!/usr/bin/env python3
"""
Hard validation for the frozen derivatives-pressure core event.

Key differences from the initial reconstruction:
- Counts only EVENT ONSETS (false -> true) to avoid treating one persistent condition
  as dozens of independent 15m events.
- Uses an explicit discovery/holdout symbol split.
- Measures raw Spot forward returns plus MFE/MAE at 24h/48h/72h.
- No threshold optimization, no trading rules, no order placement.
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
OUT = pathlib.Path("validation/edges/derivatives-pressure-hard-validation.json")
PERIOD = "15m"
LOOKBACK_DAYS = 14
BAR_MS = 15 * 60 * 1000
HORIZONS = {"24h": 96, "48h": 192, "72h": 288}
DISCOVERY_SYMBOLS = ["PUMPUSDT", "ASTERUSDT", "WLDUSDT", "ENAUSDT", "NEARUSDT", "SUIUSDT"]
HOLDOUT_SYMBOLS = ["ARBUSDT", "MARSCOINUSDT", "ZKCUSDT", "SAHARAUSDT"]
ALL_SYMBOLS = DISCOVERY_SYMBOLS + HOLDOUT_SYMBOLS


def get(base, path):
    req = urllib.request.Request(base + path, headers={
        "User-Agent": "Mozilla/5.0 tst-derivatives-pressure-hard-validation/1.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.binance.com/",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def qs(params):
    return urllib.parse.urlencode(params)


def fetch_series(path, symbol, start_ms, end_ms, limit=500):
    rows, cursor = [], start_ms
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
        time.sleep(0.03)
    dedup = {}
    for r in rows:
        ts = int(r.get("timestamp") or 0)
        if ts:
            dedup[ts] = r
    return [dedup[k] for k in sorted(dedup)]


def fetch_spot_klines(symbol, start_ms, end_ms):
    rows, cursor = [], start_ms
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
        time.sleep(0.03)
    dedup = {int(r[0]): r for r in rows}
    return [dedup[k] for k in sorted(dedup)]


def median(xs):
    return statistics.median(xs) if xs else None


def summarize(events, horizon):
    vals = [e[horizon]["ret"] for e in events if horizon in e and math.isfinite(e[horizon]["ret"])]
    mfes = [e[horizon]["mfe"] for e in events if horizon in e and math.isfinite(e[horizon]["mfe"])]
    maes = [e[horizon]["mae"] for e in events if horizon in e and math.isfinite(e[horizon]["mae"])]
    if not vals:
        return {"n": 0, "meanPct": None, "medianPct": None, "hitRatePct": None,
                "medianMfePct": None, "medianMaePct": None, "medianMfeMaeRatio": None}
    med_mfe = median(mfes)
    med_mae = median(maes)
    ratio = None if med_mae is None or med_mae <= 0 else med_mfe / med_mae
    return {
        "n": len(vals),
        "meanPct": round(100 * sum(vals) / len(vals), 4),
        "medianPct": round(100 * median(vals), 4),
        "hitRatePct": round(100 * sum(1 for x in vals if x > 0) / len(vals), 2),
        "medianMfePct": round(100 * med_mfe, 4),
        "medianMaePct": round(100 * med_mae, 4),
        "medianMfeMaeRatio": None if ratio is None else round(ratio, 4),
    }


def gate(summary):
    reasons = []
    for h in HORIZONS:
        s = summary[h]
        if s["n"] < 10:
            reasons.append(f"{h.upper()}_TOO_FEW_EVENTS")
            continue
        if s["meanPct"] is None or s["meanPct"] < 1.5:
            reasons.append(f"{h.upper()}_MEAN_BELOW_1_50")
        if s["medianPct"] is None or s["medianPct"] <= 0:
            reasons.append(f"{h.upper()}_MEDIAN_NOT_POSITIVE")
        if s["hitRatePct"] is None or s["hitRatePct"] <= 55:
            reasons.append(f"{h.upper()}_HIT_RATE_NOT_ABOVE_55")
        if s["medianMfeMaeRatio"] is None or s["medianMfeMaeRatio"] < 2.0:
            reasons.append(f"{h.upper()}_MFE_MAE_BELOW_2")
    return reasons


now = dt.datetime.now(dt.timezone.utc)
end_ms = int(now.timestamp() * 1000)
end_ms -= end_ms % BAR_MS
start_ms = end_ms - LOOKBACK_DAYS * 24 * 60 * 60 * 1000

events_by_symbol = {}
failures = {}
series_meta = {}

for symbol in ALL_SYMBOLS:
    try:
        oi = fetch_series("/futures/data/openInterestHist", symbol, start_ms, end_ms)
        taker = fetch_series("/futures/data/takerlongshortRatio", symbol, start_ms, end_ms)
        spot = fetch_spot_klines(symbol, start_ms, end_ms + 72 * 60 * 60 * 1000)
        oi_map = {int(r["timestamp"]): r for r in oi}
        taker_map = {int(r["timestamp"]): r for r in taker}
        spot_map = {int(r[0]): r for r in spot}
        timestamps = sorted(set(oi_map) & set(taker_map) & set(spot_map))
        prev_condition = False
        symbol_events = []

        for ts in timestamps:
            prev_ts = ts - 8 * BAR_MS
            if prev_ts not in oi_map:
                prev_condition = False
                continue
            oi0 = float(oi_map[prev_ts].get("sumOpenInterestValue") or oi_map[prev_ts].get("sumOpenInterest") or 0)
            oi1 = float(oi_map[ts].get("sumOpenInterestValue") or oi_map[ts].get("sumOpenInterest") or 0)
            if oi0 <= 0:
                prev_condition = False
                continue
            oi_chg = oi1 / oi0 - 1
            ratios = []
            for j in range(4):
                t = ts - j * BAR_MS
                if t in taker_map:
                    ratios.append(float(taker_map[t].get("buySellRatio") or 1))
            if len(ratios) < 4:
                prev_condition = False
                continue
            taker_ratio = sum(ratios) / len(ratios)

            score = 50
            score += 20 if oi_chg >= 0.02 else (10 if oi_chg > 0 else -10)
            score += 20 if taker_ratio >= 1.15 else (10 if taker_ratio >= 1.05 else -10)
            condition = score >= 80 and oi_chg > 0 and taker_ratio >= 1.05
            onset = condition and not prev_condition
            prev_condition = condition
            if not onset:
                continue

            entry = float(spot_map[ts][4])
            ev = {"symbol": symbol, "timestamp": ts, "oiChange2h": oi_chg,
                  "takerBuySellRatio1h": taker_ratio, "coreScore": score}
            for h, bars in HORIZONS.items():
                fts = ts + bars * BAR_MS
                if fts not in spot_map:
                    continue
                future = [spot_map.get(ts + k * BAR_MS) for k in range(1, bars + 1)]
                future = [r for r in future if r is not None]
                if not future:
                    continue
                close_px = float(spot_map[fts][4])
                max_high = max(float(r[2]) for r in future)
                min_low = min(float(r[3]) for r in future)
                ev[h] = {
                    "ret": close_px / entry - 1,
                    "mfe": max_high / entry - 1,
                    "mae": max(0.0, 1 - min_low / entry),
                }
            if any(h in ev for h in HORIZONS):
                symbol_events.append(ev)
        events_by_symbol[symbol] = symbol_events
        series_meta[symbol] = {"oiRows": len(oi), "takerRows": len(taker), "spotRows": len(spot), "eventOnsets": len(symbol_events)}
    except Exception as e:
        failures[symbol] = str(e)


def collect(symbols):
    out = []
    for s in symbols:
        out.extend(events_by_symbol.get(s, []))
    return out


def summarize_group(symbols):
    evs = collect(symbols)
    return {h: summarize(evs, h) for h in HORIZONS}


discovery_summary = summarize_group(DISCOVERY_SYMBOLS)
holdout_summary = summarize_group(HOLDOUT_SYMBOLS)
all_summary = summarize_group(ALL_SYMBOLS)
discovery_reasons = gate(discovery_summary)
holdout_reasons = gate(holdout_summary)
all_reasons = gate(all_summary)

report = {
    "schemaVersion": 1,
    "strategyId": "TST_DERIVATIVES_PRESSURE_V2_CORE_HARD_VALIDATION",
    "authorization": "RESEARCH_ONLY",
    "liveTrading": False,
    "lookbackDays": LOOKBACK_DAYS,
    "period": PERIOD,
    "frozenEventDefinition": "coreScore>=80 AND oiChange2h>0 AND takerBuySellRatio1h>=1.05; count only false->true event onset",
    "discoverySymbols": DISCOVERY_SYMBOLS,
    "holdoutSymbols": HOLDOUT_SYMBOLS,
    "seriesMeta": series_meta,
    "discovery": discovery_summary,
    "holdout": holdout_summary,
    "all": all_summary,
    "validation": {
        "passed": not discovery_reasons and not holdout_reasons and not all_reasons,
        "discoveryReasons": discovery_reasons,
        "holdoutReasons": holdout_reasons,
        "allReasons": all_reasons,
    },
    "failures": failures,
    "generatedAt": now.isoformat(),
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
