#!/usr/bin/env python3
"""Research-only validation of extreme liquidation shocks on Binance Vision.

Uses Coinalyze only to define the historical liquidation-shock dates, then
validates market-state confirmation and forward returns from Binance's official
public archive:
  - USD-M 15m klines (price + taker-buy quote volume)
  - USD-M metrics (open interest)

No threshold optimization is performed. Fixed definitions:
  * shock: global top 1% or top 2% of daily liquidation notional
  * side dominance: abs(liq_imbalance) >= 0.50
  * OI direction: sign of end-of-day vs start-of-day OI value
  * OI acceleration: second-half OI return minus first-half OI return
  * taker flow: buy quote volume / total quote volume, split at 0.50

Signal time is the close of the shock day. Forward returns use Binance Vision
close-to-close 1d/2d/3d returns, avoiding look-ahead from future-day features.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import os
import pathlib
import statistics
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict

AUTHORIZATION = "RESEARCH_ONLY"
BASE = "https://data.binance.vision/data/futures/um/daily"
SRC = pathlib.Path(os.getenv("COINALYZE_DISCOVERY_DIR", "/data/coinalyze-regime-discovery")) / "coinalyze_daily_regime_dataset.csv"
OUT = pathlib.Path(os.getenv("BINANCE_VISION_SHOCK_DIR", "/data/binance-vision-extreme-shocks"))
KLINE_INTERVAL = "15m"
DOMINANCE = 0.50
UA = "tst-research-binance-vision-validation/1.0"


def qtile(xs, q):
    ys = sorted(float(x) for x in xs if x is not None and math.isfinite(float(x)))
    if not ys:
        return 0.0
    p = (len(ys) - 1) * q
    lo, hi = math.floor(p), math.ceil(p)
    if lo == hi:
        return ys[lo]
    w = p - lo
    return ys[lo] * (1 - w) + ys[hi] * w


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


def download_zip_csv(url: str, retries: int = 3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if not names:
                    raise RuntimeError(f"no CSV in {url}")
                text = zf.read(names[0]).decode("utf-8-sig", errors="replace")
                return list(csv.reader(io.StringIO(text)))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last = exc
        except Exception as exc:
            last = exc
        time.sleep(1.0 + i)
    raise RuntimeError(f"download failed: {url}: {last}")


def kline_url(symbol: str, day: str) -> str:
    return f"{BASE}/klines/{symbol}/{KLINE_INTERVAL}/{symbol}-{KLINE_INTERVAL}-{day}.zip"


def metrics_url(symbol: str, day: str) -> str:
    return f"{BASE}/metrics/{symbol}/{symbol}-metrics-{day}.zip"


def parse_klines(rows):
    if not rows:
        return []
    out = []
    for r in rows:
        if not r or not str(r[0]).strip().isdigit():
            continue
        if len(r) < 11:
            continue
        try:
            out.append({
                "open_ms": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
                "quote_volume": float(r[7]),
                "trades": int(float(r[8])),
                "taker_buy_base": float(r[9]),
                "taker_buy_quote": float(r[10]),
            })
        except Exception:
            continue
    return out


def parse_metrics(rows):
    if not rows:
        return []
    # Metrics archives have a header. Parse flexibly because Binance has evolved fields.
    header = [str(x).strip() for x in rows[0]]
    if "create_time" not in header:
        return []
    idx = {k: i for i, k in enumerate(header)}
    oi_col = "sum_open_interest_value" if "sum_open_interest_value" in idx else "sum_open_interest"
    if oi_col not in idx:
        return []
    out = []
    for r in rows[1:]:
        try:
            ts = str(r[idx["create_time"]]).strip()
            value = float(r[idx[oi_col]])
            out.append((ts, value))
        except Exception:
            continue
    return out


def day_state(symbol: str, day: str, cache: dict):
    key = (symbol, day)
    if key in cache:
        return cache[key]
    kres = download_zip_csv(kline_url(symbol, day))
    mres = download_zip_csv(metrics_url(symbol, day))
    kl = parse_klines(kres)
    mt = parse_metrics(mres)
    if not kl:
        cache[key] = None
        return None
    quote = sum(x["quote_volume"] for x in kl)
    taker = sum(x["taker_buy_quote"] for x in kl)
    buy_share = (taker / quote) if quote > 0 else None
    close = kl[-1]["close"]
    open_px = kl[0]["open"]
    out = {
        "open": open_px,
        "close": close,
        "day_return": (close / open_px - 1.0) if open_px > 0 else None,
        "taker_buy_share": buy_share,
        "quote_volume": quote,
        "bars": len(kl),
        "metrics_rows": len(mt),
        "oi_start": None,
        "oi_mid": None,
        "oi_end": None,
        "oi_change": None,
        "oi_accel": None,
    }
    if len(mt) >= 3:
        start = mt[0][1]
        mid = mt[len(mt)//2][1]
        end = mt[-1][1]
        out["oi_start"], out["oi_mid"], out["oi_end"] = start, mid, end
        if start > 0 and mid > 0:
            first = mid / start - 1.0
            second = end / mid - 1.0
            out["oi_change"] = end / start - 1.0
            out["oi_accel"] = second - first
    cache[key] = out
    return out


def load_source():
    if not SRC.exists():
        raise SystemExit(f"missing source dataset: {SRC}")
    rows = []
    with SRC.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "symbol": r["symbol"],
                    "day": r["day"],
                    "liq_total": float(r["liq_total"]),
                    "liq_imbalance": float(r["liq_imbalance"]),
                })
            except Exception:
                continue
    return rows


def main():
    src = load_source()
    vals = [r["liq_total"] for r in src]
    p98 = qtile(vals, 0.98)
    p99 = qtile(vals, 0.99)
    shocks = [r for r in src if r["liq_total"] >= p98]
    cache = {}
    validated = []
    download_failures = []

    for s in shocks:
        d0 = dt.date.fromisoformat(s["day"])
        try:
            states = []
            for h in range(4):
                day = (d0 + dt.timedelta(days=h)).isoformat()
                states.append(day_state(s["symbol"], day, cache))
        except Exception as exc:
            download_failures.append({"symbol": s["symbol"], "day": s["day"], "error": f"{type(exc).__name__}: {exc}"})
            continue
        cur = states[0]
        if cur is None:
            download_failures.append({"symbol": s["symbol"], "day": s["day"], "error": "missing shock-day kline"})
            continue
        row = {**s, **cur}
        row["top1"] = s["liq_total"] >= p99
        row["top2"] = True
        row["long_dominant"] = s["liq_imbalance"] <= -DOMINANCE
        row["short_dominant"] = s["liq_imbalance"] >= DOMINANCE
        row["oi_down"] = cur["oi_change"] is not None and cur["oi_change"] < 0
        row["oi_up"] = cur["oi_change"] is not None and cur["oi_change"] > 0
        row["oi_accel_down"] = cur["oi_accel"] is not None and cur["oi_accel"] < 0
        row["oi_accel_up"] = cur["oi_accel"] is not None and cur["oi_accel"] > 0
        row["buy_flow"] = cur["taker_buy_share"] is not None and cur["taker_buy_share"] > 0.50
        row["sell_flow"] = cur["taker_buy_share"] is not None and cur["taker_buy_share"] < 0.50
        for h in (1, 2, 3):
            st = states[h]
            row[f"fwd_{h}d"] = (st["close"] / cur["close"] - 1.0) if st and cur["close"] > 0 else None
        validated.append(row)

    # Pre-registered, sign-based confirmation families only.
    regimes = {
        "top2_all": lambda r: r["top2"],
        "top1_all": lambda r: r["top1"],
        "top2_long_dom": lambda r: r["long_dominant"],
        "top2_short_dom": lambda r: r["short_dominant"],
        "top2_long_dom_oi_down": lambda r: r["long_dominant"] and r["oi_down"],
        "top2_long_dom_sell_flow": lambda r: r["long_dominant"] and r["sell_flow"],
        "top2_long_dom_oi_down_sell_flow": lambda r: r["long_dominant"] and r["oi_down"] and r["sell_flow"],
        "top2_long_dom_oi_down_sell_flow_accel_down": lambda r: r["long_dominant"] and r["oi_down"] and r["sell_flow"] and r["oi_accel_down"],
        "top2_short_dom_oi_down": lambda r: r["short_dominant"] and r["oi_down"],
        "top2_short_dom_buy_flow": lambda r: r["short_dominant"] and r["buy_flow"],
        "top2_short_dom_oi_down_buy_flow": lambda r: r["short_dominant"] and r["oi_down"] and r["buy_flow"],
        "top2_short_dom_oi_down_buy_flow_accel_up": lambda r: r["short_dominant"] and r["oi_down"] and r["buy_flow"] and r["oi_accel_up"],
        "top1_long_dom_oi_down_sell_flow": lambda r: r["top1"] and r["long_dominant"] and r["oi_down"] and r["sell_flow"],
        "top1_short_dom_oi_down_buy_flow": lambda r: r["top1"] and r["short_dominant"] and r["oi_down"] and r["buy_flow"],
    }
    report = {}
    for name, pred in regimes.items():
        selected = [r for r in validated if pred(r)]
        report[name] = {
            "rows": len(selected),
            "fwd1d": summarize(r.get("fwd_1d") for r in selected),
            "fwd2d": summarize(r.get("fwd_2d") for r in selected),
            "fwd3d": summarize(r.get("fwd_3d") for r in selected),
        }

    OUT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT / "binance_vision_extreme_shocks.csv"
    keys = [
        "symbol","day","liq_total","liq_imbalance","top1","top2","long_dominant","short_dominant",
        "open","close","day_return","taker_buy_share","quote_volume","bars","metrics_rows",
        "oi_start","oi_mid","oi_end","oi_change","oi_accel","oi_down","oi_up","oi_accel_down","oi_accel_up",
        "buy_flow","sell_flow","fwd_1d","fwd_2d","fwd_3d",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in validated:
            w.writerow({k: r.get(k) for k in keys})

    payload = {
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "source": "Binance Vision validation; Coinalyze used only for liquidation shock-date definition",
        "klineInterval": KLINE_INTERVAL,
        "shockThresholds": {"p98": p98, "p99": p99},
        "dominanceThreshold": DOMINANCE,
        "sourceRows": len(src),
        "shockRowsRequested": len(shocks),
        "validatedRows": len(validated),
        "downloadFailures": download_failures,
        "regimes": report,
        "gate": {
            "grossMeanRequired": 0.015,
            "medianPositiveRequired": True,
            "hitRateRequired": 0.55,
            "note": "No candidate promotion from this validation unless predefined gate is satisfied with adequate sample size and independent OOS replication."
        },
    }
    report_path = OUT / "binance_vision_extreme_shocks_report.json"
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"kind":"binance_vision_extreme_shock_validation_complete","dataset":str(csv_path),"report":str(report_path), **payload}, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
