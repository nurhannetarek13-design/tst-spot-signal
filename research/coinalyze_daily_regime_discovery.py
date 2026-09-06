#!/usr/bin/env python3
"""Research-only daily derivatives regime discovery using Coinalyze.

Uses the same Binance USD-M perpetual symbols and daily buckets for:
- liquidations (long/short aggregate USD value)
- open interest history (USD converted)
- funding-rate history
- OHLCV history

This is a discovery layer only. Any promising regime must later be validated
against the canonical Binance Vision / independent execution stack.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import pathlib
import statistics
from collections import defaultdict

from coinalyze_liquidation_history import (
    AUTHORIZATION,
    API_KEY_ENV,
    DEFAULT_SYMBOLS,
    api_get,
    discover_binance_code,
    discover_symbols,
    unix_seconds,
    utc_now,
)

DAYS = int(os.getenv("COINALYZE_DISCOVERY_DAYS", "365"))
OUT_DIR = pathlib.Path(os.getenv("COINALYZE_DISCOVERY_DIR", "/data/coinalyze-regime-discovery"))
INTERVAL = "daily"


def history_map(payload, symbol_map, fields):
    reverse = {v: k for k, v in symbol_map.items()}
    out = defaultdict(dict)
    for block in payload:
        cz = str(block.get("symbol") or "")
        sym = reverse.get(cz, cz)
        for p in block.get("history") or []:
            t = int(p.get("t") or 0)
            if not t:
                continue
            day = dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).date().isoformat()
            row = {k: float(p.get(src) or 0.0) for k, src in fields.items()}
            out[sym][day] = row
    return out


def qtile(xs, q):
    ys = sorted(xs)
    if not ys:
        return 0.0
    pos = (len(ys) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return float(ys[lo])
    w = pos - lo
    return float(ys[lo] * (1 - w) + ys[hi] * w)


def summarize(values):
    vals = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    if not vals:
        return {"n": 0, "mean": None, "median": None, "hitRate": None}
    return {
        "n": len(vals),
        "mean": sum(vals) / len(vals),
        "median": statistics.median(vals),
        "hitRate": sum(1 for x in vals if x > 0) / len(vals),
    }


def main():
    api_key = os.getenv(API_KEY_ENV, "").strip()
    if not api_key:
        raise SystemExit(f"Missing {API_KEY_ENV}")

    symbols = tuple(s.strip().upper() for s in os.getenv("LIQ_SYMBOLS", ",".join(DEFAULT_SYMBOLS)).split(",") if s.strip())
    end = utc_now().replace(microsecond=0)
    start = end - dt.timedelta(days=DAYS + 5)

    code = discover_binance_code(api_key)
    smap = discover_symbols(api_key, symbols, code)
    syms_csv = ",".join(smap.values())
    common = {"symbols": syms_csv, "interval": INTERVAL, "from": unix_seconds(start), "to": unix_seconds(end)}

    liq = history_map(api_get("liquidation-history", api_key, {**common, "convert_to_usd": "true"}), smap, {"long_liq": "l", "short_liq": "s"})
    oi = history_map(api_get("open-interest-history", api_key, {**common, "convert_to_usd": "true"}), smap, {"oi_open": "o", "oi_high": "h", "oi_low": "l", "oi_close": "c"})
    fr = history_map(api_get("funding-rate-history", api_key, common), smap, {"funding_open": "o", "funding_high": "h", "funding_low": "l", "funding_close": "c"})
    px = history_map(api_get("ohlcv-history", api_key, common), smap, {"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v", "buy_volume": "bv", "trades": "tx", "buy_trades": "btx"})

    rows = []
    for sym in symbols:
        days = sorted(set(liq[sym]) & set(oi[sym]) & set(fr[sym]) & set(px[sym]))
        for day in days:
            r = {"symbol": sym, "day": day, **liq[sym][day], **oi[sym][day], **fr[sym][day], **px[sym][day]}
            total = r["long_liq"] + r["short_liq"]
            r["liq_total"] = total
            r["liq_imbalance"] = ((r["short_liq"] - r["long_liq"]) / total) if total > 0 else 0.0
            r["taker_buy_share"] = (r["buy_volume"] / r["volume"]) if r["volume"] > 0 else None
            rows.append(r)

    by_sym = defaultdict(list)
    for r in rows:
        by_sym[r["symbol"]].append(r)
    for sym, arr in by_sym.items():
        arr.sort(key=lambda r: r["day"])
        for i, r in enumerate(arr):
            prev = arr[i-1] if i > 0 else None
            r["oi_change_1d"] = ((r["oi_close"] / prev["oi_close"] - 1.0) if prev and prev["oi_close"] > 0 else None)
            for h in (1, 2, 3):
                fut = arr[i+h] if i + h < len(arr) else None
                r[f"fwd_{h}d"] = ((fut["close"] / r["close"] - 1.0) if fut and r["close"] > 0 else None)

    liq_vals = [r["liq_total"] for r in rows]
    liq_p80 = qtile(liq_vals, 0.80)
    liq_p95 = qtile(liq_vals, 0.95)

    regimes = {
        "all": lambda r: True,
        "liq_top20": lambda r: r["liq_total"] >= liq_p80,
        "liq_top5": lambda r: r["liq_total"] >= liq_p95,
        "long_liq_dominant": lambda r: r["liq_imbalance"] <= -0.50,
        "short_liq_dominant": lambda r: r["liq_imbalance"] >= 0.50,
        "liq_top20_long_dominant": lambda r: r["liq_total"] >= liq_p80 and r["liq_imbalance"] <= -0.50,
        "liq_top20_short_dominant": lambda r: r["liq_total"] >= liq_p80 and r["liq_imbalance"] >= 0.50,
        "liq_top20_oi_up": lambda r: r["liq_total"] >= liq_p80 and (r["oi_change_1d"] or 0) > 0,
        "liq_top20_oi_down": lambda r: r["liq_total"] >= liq_p80 and (r["oi_change_1d"] or 0) < 0,
        "liq_top20_funding_pos": lambda r: r["liq_total"] >= liq_p80 and r["funding_close"] > 0,
        "liq_top20_funding_neg": lambda r: r["liq_total"] >= liq_p80 and r["funding_close"] < 0,
        "long_liq_oi_down": lambda r: r["liq_imbalance"] <= -0.50 and (r["oi_change_1d"] or 0) < 0,
        "short_liq_oi_down": lambda r: r["liq_imbalance"] >= 0.50 and (r["oi_change_1d"] or 0) < 0,
    }

    report = {}
    for name, pred in regimes.items():
        selected = [r for r in rows if pred(r)]
        report[name] = {
            "rows": len(selected),
            "fwd1d": summarize(r.get("fwd_1d") for r in selected),
            "fwd2d": summarize(r.get("fwd_2d") for r in selected),
            "fwd3d": summarize(r.get("fwd_3d") for r in selected),
        }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    merged = OUT_DIR / "coinalyze_daily_regime_dataset.csv"
    keys = [
        "symbol","day","long_liq","short_liq","liq_total","liq_imbalance",
        "oi_open","oi_high","oi_low","oi_close","oi_change_1d",
        "funding_open","funding_high","funding_low","funding_close",
        "open","high","low","close","volume","buy_volume","taker_buy_share",
        "trades","buy_trades","fwd_1d","fwd_2d","fwd_3d",
    ]
    with merged.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})

    payload = {
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "source": "Coinalyze discovery layer",
        "exchange": "Binance",
        "exchangeCode": code,
        "symbolMap": smap,
        "interval": INTERVAL,
        "daysRequested": DAYS,
        "rows": len(rows),
        "rowsBySymbol": {s: len(by_sym[s]) for s in symbols},
        "liqP80": liq_p80,
        "liqP95": liq_p95,
        "regimes": report,
        "validationRequired": "Any promising regime must be validated on Binance Vision / independent OOS before candidate promotion.",
    }
    report_path = OUT_DIR / "coinalyze_daily_regime_report.json"
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"kind":"coinalyze_regime_discovery_complete","dataset":str(merged),"report":str(report_path), **payload}, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
