#!/usr/bin/env python3
"""Long-window historical research harness for TST_DERIVATIVES_PRESSURE_V2.

Uses only public, read-only Binance Vision archives. No order placement, no futures
execution, no leverage. The event is reconstructed from archived USD-M metrics
(open interest + taker long/short volume ratio) and Spot 15m closes.
"""
import datetime as dt
import json
import math
import os
import pathlib
import statistics

from binance_vision_archive import load_klines, load_um_metrics

OUT = pathlib.Path("validation/edges/derivatives-pressure-backtest.json")
SYMBOLS = ["PUMPUSDT", "ASTERUSDT", "WLDUSDT", "ENAUSDT", "NEARUSDT", "SUIUSDT"]
PERIOD = "15m"
LOOKBACK_DAYS = int(os.environ.get("TST_LOOKBACK_DAYS", "90"))
MIN_METRICS_COVERAGE_PCT = float(os.environ.get("TST_MIN_METRICS_COVERAGE_PCT", "95"))
BAR_MS = 15 * 60 * 1000
HORIZONS = {"24h": 96, "48h": 192, "72h": 288}


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


def mfe_mae(spot_map, ts, entry, bars):
    highs, lows = [], []
    for j in range(1, bars + 1):
        row = spot_map.get(ts + j * BAR_MS)
        if row is None:
            continue
        highs.append(float(row[2]) / entry - 1)
        lows.append(float(row[3]) / entry - 1)
    if not highs or not lows:
        return None, None
    return max(highs), min(lows)


now = dt.datetime.now(dt.timezone.utc)
end_ms = int(now.timestamp() * 1000)
end_ms -= end_ms % BAR_MS
# Archives are complete only through the previous UTC day. Avoid blending an API tail
# into an otherwise deterministic archive run.
today_ms = int(dt.datetime(now.year, now.month, now.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
end_ms = min(end_ms, today_ms)
start_ms = end_ms - LOOKBACK_DAYS * 24 * 60 * 60 * 1000
spot_end_ms = end_ms + 72 * 60 * 60 * 1000

all_events = []
failures = {}
per_symbol = {}
quality_reasons = []

for symbol in SYMBOLS:
    try:
        metrics, metric_quality = load_um_metrics(symbol, start_ms, end_ms)
        spot = load_klines("spot", symbol, PERIOD, start_ms, spot_end_ms)
        metrics_map = {int(r["timestamp"]): r for r in metrics}
        spot_map = {int(r[0]): r for r in spot}

        # Metrics are 5m snapshots. The strategy is frozen on 15m, so sample only exact
        # 15m boundaries to preserve causal alignment and avoid nearest-neighbor leakage.
        timestamps = sorted(ts for ts in set(metrics_map) & set(spot_map) if ts % BAR_MS == 0)
        events = []
        symbol_quality_reasons = []
        coverage = metric_quality.get("coveragePctApprox")
        if coverage is None or coverage < MIN_METRICS_COVERAGE_PCT:
            symbol_quality_reasons.append("METRICS_COVERAGE_BELOW_GATE")
            quality_reasons.append(f"{symbol}_METRICS_COVERAGE_BELOW_{MIN_METRICS_COVERAGE_PCT:g}")
        if metric_quality.get("missingArchiveDays"):
            symbol_quality_reasons.append("MISSING_METRICS_ARCHIVE_DAYS")

        for ts in timestamps:
            prev_ts = ts - 8 * BAR_MS  # 2h OI change
            if prev_ts not in metrics_map:
                continue
            oi0 = float(metrics_map[prev_ts].get("sumOpenInterestValue") or metrics_map[prev_ts].get("sumOpenInterest") or 0)
            oi1 = float(metrics_map[ts].get("sumOpenInterestValue") or metrics_map[ts].get("sumOpenInterest") or 0)
            if oi0 <= 0:
                continue
            oi_chg = oi1 / oi0 - 1

            # Four 15m observations = 1h average. The archived field is Binance's
            # sum_taker_long_short_vol_ratio, mapped to buySellRatio by the loader.
            ratios = []
            for j in range(4):
                t = ts - j * BAR_MS
                row = metrics_map.get(t)
                if row is not None:
                    try:
                        ratios.append(float(row.get("buySellRatio") or 0))
                    except (TypeError, ValueError):
                        pass
            if len(ratios) < 4:
                continue
            taker_ratio = sum(ratios) / len(ratios)

            score = 50
            score += 20 if oi_chg >= 0.02 else (10 if oi_chg > 0 else -10)
            score += 20 if taker_ratio >= 1.15 else (10 if taker_ratio >= 1.05 else -10)
            core_event = score >= 80 and oi_chg > 0 and taker_ratio >= 1.05
            if not core_event:
                continue

            entry = float(spot_map[ts][4])
            ev = {
                "symbol": symbol,
                "timestamp": ts,
                "oiChange2h": oi_chg,
                "takerBuySellRatio1h": taker_ratio,
                "coreScore": score,
            }
            valid_any = False
            for name, bars in HORIZONS.items():
                fts = ts + bars * BAR_MS
                if fts in spot_map:
                    px = float(spot_map[fts][4])
                    ev[name] = px / entry - 1
                    mfe, mae = mfe_mae(spot_map, ts, entry, bars)
                    if mfe is not None:
                        ev[name + "MFE"] = mfe
                        ev[name + "MAE"] = mae
                    valid_any = True
            if valid_any:
                events.append(ev)
                all_events.append(ev)

        per_symbol[symbol] = {
            "metricsRows": len(metrics),
            "spotRows": len(spot),
            "aligned15mRows": len(timestamps),
            "events": len(events),
            "metricsQuality": metric_quality,
            "qualityReasons": symbol_quality_reasons,
        }
    except Exception as e:
        failures[symbol] = f"{type(e).__name__}: {e}"

summary = {}
mfe_mae_summary = {}
for h in HORIZONS:
    vals = [e[h] for e in all_events if h in e and math.isfinite(e[h])]
    summary[h] = summarize(vals)
    mfes = [e[h + "MFE"] for e in all_events if h + "MFE" in e and math.isfinite(e[h + "MFE"])]
    maes = [abs(e[h + "MAE"]) for e in all_events if h + "MAE" in e and math.isfinite(e[h + "MAE"])]
    med_mfe = median(mfes)
    med_mae = median(maes)
    mfe_mae_summary[h] = {
        "n": min(len(mfes), len(maes)),
        "medianMfePct": round(100 * med_mfe, 4) if med_mfe is not None else None,
        "medianMaePct": round(100 * med_mae, 4) if med_mae is not None else None,
        "ratio": round(med_mfe / med_mae, 4) if med_mfe is not None and med_mae and med_mae > 0 else None,
    }

gate_reasons = list(quality_reasons)
if failures:
    gate_reasons.append("SYMBOL_DOWNLOAD_OR_PARSE_FAILURES")
for h in ("24h", "48h", "72h"):
    s = summary[h]
    m = mfe_mae_summary[h]
    if not s["n"]:
        gate_reasons.append(f"{h.upper()}_NO_EVENTS")
        continue
    if s["meanPct"] is None or s["meanPct"] < 1.5:
        gate_reasons.append(f"{h.upper()}_MEAN_BELOW_1_50")
    if s["medianPct"] is None or s["medianPct"] <= 0:
        gate_reasons.append(f"{h.upper()}_MEDIAN_NOT_POSITIVE")
    if s["hitRatePct"] is None or s["hitRatePct"] <= 55:
        gate_reasons.append(f"{h.upper()}_HIT_RATE_NOT_ABOVE_55")
    if m["ratio"] is None or m["ratio"] < 2.0:
        gate_reasons.append(f"{h.upper()}_MFE_MAE_BELOW_2_0")

report = {
    "schemaVersion": 2,
    "strategyId": "TST_DERIVATIVES_PRESSURE_V2_CORE_HISTORICAL",
    "authorization": "RESEARCH_ONLY",
    "liveTrading": False,
    "dataSource": "BINANCE_VISION_ARCHIVES",
    "checksumVerification": True,
    "timestampNormalization": "SPOT_MICROSECONDS_OR_MILLISECONDS_TO_MILLISECONDS",
    "lookbackDays": LOOKBACK_DAYS,
    "period": PERIOD,
    "symbols": SYMBOLS,
    "historicalFeatureScope": {
        "openInterest": True,
        "takerBuySellRatio": True,
        "funding": False,
        "basis": False,
        "note": "OI and taker ratio come from USD-M daily metrics archives; Spot forward returns come from verified 15m archive klines."
    },
    "eventDefinition": "coreScore>=80 AND oiChange2h>0 AND takerBuySellRatio1h>=1.05",
    "perSymbol": per_symbol,
    "eventCount": len(all_events),
    "forwardReturns": summary,
    "mfeMae": mfe_mae_summary,
    "validation": {"passed": not gate_reasons, "reasons": sorted(set(gate_reasons))},
    "failures": failures,
    "generatedAt": now.isoformat(),
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
