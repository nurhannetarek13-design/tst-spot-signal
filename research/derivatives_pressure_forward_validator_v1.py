#!/usr/bin/env python3
"""Forward validator for committed Derivatives Pressure snapshots.

Research-only. Reconstructs historical Score>=90 paperLongPressure episodes from
Git history, de-clusters repeated snapshots, and measures subsequent Spot
returns from the next 15m open. No parameter optimization and no live actions.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import statistics
import subprocess
import time
import urllib.parse
import urllib.request

SNAPSHOT_PATH = "validation/edges/derivatives-pressure-latest.json"
OUT = pathlib.Path("validation/edges/derivatives-pressure-forward-validation-latest.json")
BASE = "https://data-api.binance.vision"
HORIZONS_H = (1, 4, 12, 24)
NORMAL_COST = 0.0028
STRESS_COST = 0.0050
MIN_SCORE = 90
DECLUSTER_HOURS = 8
MIN_EVENTS_FOR_VERDICT = 30
RETRIES = 4


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL)


def parse_ts(s: str) -> dt.datetime:
    x = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if x.tzinfo is None:
        x = x.replace(tzinfo=dt.timezone.utc)
    return x.astimezone(dt.timezone.utc)


def load_snapshots():
    shas = [x.strip() for x in git("log", "--format=%H", "--reverse", "--", SNAPSHOT_PATH).splitlines() if x.strip()]
    rows = []
    bad = []
    for sha in shas:
        try:
            raw = git("show", f"{sha}:{SNAPSHOT_PATH}")
            d = json.loads(raw)
            ts = parse_ts(d["generatedAt"])
            candidates = {}
            for c in d.get("candidates") or []:
                sym = str(c.get("symbol") or "")
                if not sym:
                    continue
                candidates[sym] = {
                    "score": float(c.get("score") or 0),
                    "paperLongPressure": bool(c.get("paperLongPressure")),
                    "price": float(c.get("price") or 0),
                    "oiChange2h": c.get("oiChange2h"),
                    "takerBuySellRatio1h": c.get("takerBuySellRatio1h"),
                    "fundingRate": c.get("fundingRate"),
                    "basis": c.get("basis"),
                }
            rows.append({"sha": sha, "ts": ts, "candidates": candidates})
        except Exception as exc:
            bad.append({"sha": sha, "error": f"{type(exc).__name__}: {exc}"})
    rows.sort(key=lambda x: x["ts"])
    return rows, bad


def pressure_events(snapshots):
    raw = []
    for s in snapshots:
        for sym, c in s["candidates"].items():
            if c["score"] >= MIN_SCORE and c["paperLongPressure"]:
                raw.append({
                    "symbol": sym,
                    "signalTime": s["ts"],
                    "snapshotSha": s["sha"],
                    **c,
                })
    raw.sort(key=lambda x: (x["symbol"], x["signalTime"]))
    events = []
    last_true = {}
    for e in raw:
        prev = last_true.get(e["symbol"])
        if prev is None or (e["signalTime"] - prev).total_seconds() > DECLUSTER_HOURS * 3600:
            events.append(e)
        last_true[e["symbol"]] = e["signalTime"]
    events.sort(key=lambda x: x["signalTime"])
    return raw, events


def api_klines(symbol: str, start: dt.datetime, end: dt.datetime):
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": "15m",
        "startTime": int(start.timestamp() * 1000),
        "endTime": int(end.timestamp() * 1000),
        "limit": 1000,
    })
    url = BASE + "/api/v3/klines?" + params
    last = None
    for k in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tst-derivatives-forward-validator/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as exc:
            last = exc
            time.sleep(min(4.0, 0.5 * (2 ** k)))
    raise RuntimeError(f"{symbol}: klines failed: {last}")


def score_event(e, now):
    signal = e["signalTime"]
    start = signal - dt.timedelta(minutes=15)
    end = min(now, signal + dt.timedelta(hours=max(HORIZONS_H) + 2))
    rows = api_klines(e["symbol"], start, end)
    bars = []
    for r in rows:
        bars.append({
            "ot": dt.datetime.fromtimestamp(int(r[0]) / 1000, tz=dt.timezone.utc),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
        })
    entry_i = next((i for i, b in enumerate(bars) if b["ot"] >= signal), None)
    if entry_i is None:
        return {**e, "status": "NO_ENTRY_BAR"}
    entry = bars[entry_i]["open"]
    entry_time = bars[entry_i]["ot"]
    out = {
        **e,
        "signalTime": signal.isoformat(),
        "entryTime": entry_time.isoformat(),
        "entry": entry,
        "status": "OK",
        "horizons": {},
    }
    for h in HORIZONS_H:
        need = h * 4
        end_i = entry_i + need - 1
        if end_i >= len(bars):
            out["horizons"][str(h)] = {"mature": False}
            continue
        seg = bars[entry_i:end_i + 1]
        gross = seg[-1]["close"] / entry - 1.0
        mfe = max(x["high"] for x in seg) / entry - 1.0
        mae = max(0.0, 1.0 - min(x["low"] for x in seg) / entry)
        out["horizons"][str(h)] = {
            "mature": True,
            "gross": gross,
            "netNormal": gross - NORMAL_COST,
            "netStress": gross - STRESS_COST,
            "mfe": mfe,
            "mae": mae,
        }
    return out


def finite(xs):
    return [float(x) for x in xs if x is not None and math.isfinite(float(x))]


def summarize(events, h):
    rows = [e["horizons"][str(h)] for e in events if e.get("status") == "OK" and e["horizons"].get(str(h), {}).get("mature")]
    vals = finite(r["netNormal"] for r in rows)
    stress = finite(r["netStress"] for r in rows)
    mfes = finite(r["mfe"] for r in rows)
    maes = finite(r["mae"] for r in rows)
    if not vals:
        return {"n": 0, "pass": False}
    pos = sum(x for x in vals if x > 0)
    neg = -sum(x for x in vals if x < 0)
    pf = pos / neg if neg > 0 else (float("inf") if pos > 0 else 0.0)
    med_mae = statistics.median(maes) if maes else 0.0
    ratio = statistics.median(mfes) / med_mae if med_mae > 0 and mfes else None
    row = {
        "n": len(vals),
        "meanNetNormal": statistics.fmean(vals),
        "medianNetNormal": statistics.median(vals),
        "hitRateNetNormal": sum(x > 0 for x in vals) / len(vals),
        "profitFactorNetNormal": pf,
        "meanNetStress": statistics.fmean(stress),
        "medianMFE": statistics.median(mfes) if mfes else None,
        "medianMAE": med_mae if maes else None,
        "mfeMaeRatio": ratio,
    }
    row["pass"] = bool(
        row["n"] >= MIN_EVENTS_FOR_VERDICT
        and row["meanNetNormal"] > 0
        and row["medianNetNormal"] > 0
        and row["hitRateNetNormal"] > 0.55
        and row["profitFactorNetNormal"] >= 1.20
        and row["meanNetStress"] > 0
    )
    return row


def main():
    snapshots, snapshot_failures = load_snapshots()
    raw_signals, episodes = pressure_events(snapshots)
    now = dt.datetime.now(dt.timezone.utc)
    scored = []
    failures = []
    for e in episodes:
        try:
            scored.append(score_event(e, now))
        except Exception as exc:
            failures.append({"symbol": e["symbol"], "signalTime": e["signalTime"].isoformat(), "error": f"{type(exc).__name__}: {exc}"})
    metrics = {str(h): summarize(scored, h) for h in HORIZONS_H}
    mature_24h = metrics["24"]["n"]
    any_pass = any(x.get("pass") for x in metrics.values())
    status = "COLLECTING" if mature_24h < MIN_EVENTS_FOR_VERDICT else ("PASS" if any_pass else "FAIL")
    report = {
        "engine": "DERIVATIVES_PRESSURE_FORWARD_VALIDATOR_V1",
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "automaticPromotion": False,
        "status": status,
        "source": {
            "snapshotPath": SNAPSHOT_PATH,
            "snapshotCount": len(snapshots),
            "snapshotFailures": snapshot_failures,
        },
        "frozenEventDefinition": {
            "scoreMin": MIN_SCORE,
            "paperLongPressure": True,
            "declusterHours": DECLUSTER_HOURS,
            "entry": "NEXT_15M_OPEN",
        },
        "costs": {"normalRoundTrip": NORMAL_COST, "stressRoundTrip": STRESS_COST},
        "verdictGate": {
            "minEvents": MIN_EVENTS_FOR_VERDICT,
            "meanNetNormal": ">0",
            "medianNetNormal": ">0",
            "hitRateNetNormal": ">0.55",
            "profitFactorNetNormal": ">=1.20",
            "meanNetStress": ">0",
        },
        "rawPressureSnapshots": len(raw_signals),
        "episodes": len(episodes),
        "mature24h": mature_24h,
        "metrics": metrics,
        "eventFailures": failures,
        "events": scored,
        "generatedAt": now.isoformat(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    print(json.dumps({
        "kind": "derivatives_pressure_forward_validation_complete",
        "status": status,
        "snapshots": len(snapshots),
        "episodes": len(episodes),
        "mature24h": mature_24h,
        "metrics": metrics,
    }, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
