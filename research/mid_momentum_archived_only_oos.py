#!/usr/bin/env python3
"""Frozen OOS replication of Mid-Momentum V1 on archived-only Binance Spot symbols.

Purpose
-------
Test the exact three Mid-Momentum setup families and three TP/SL contracts from
`research/mid_momentum_continuation_v1.py` on symbols that had recent Binance
Vision 15m history but are no longer in the current Binance Spot universe.

Important: this is RESEARCH_ONLY. It never changes live bot rules, never sends
orders, and never promotes a strategy. The setup definitions and contracts are
imported unchanged from V1; this script only changes the evaluation universe.

Design
------
- Target OOS symbols are frozen from the prior archived-only audit: 41 symbols
  with at least two monthly 15m archives in Mar-Aug 2026.
- Target-symbol bars come from official Binance Vision monthly Spot archives.
- A current 70-symbol reference panel is loaded with the existing research
  loader only to make the cross-sectional ranks less distorted than ranking the
  delisted names against themselves.
- All data are truncated to the common historical window 2026-03-01 through
  2026-08-31 23:59 UTC.
- Signals are evaluated only on the archived-only target symbols.
- Entry is NEXT_15M_OPEN; same-bar TP+SL resolves SL_FIRST_PESSIMISTIC.
- Costs, declustering, barriers, masks, and contracts are exactly V1.
- Pass gate is frozen to V1-style validation: n>=20, positive mean net,
  PF>=1.05 under normal AND stress costs, plus day-cluster bootstrap p05>0.
"""
from __future__ import annotations

import csv
import io
import json
import pathlib
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from research import intraday_barrier_edge_scanner_v1 as base
from research import mid_momentum_continuation_v1 as mid

AUTHORIZATION = "RESEARCH_ONLY"
LIVE_TRADING = False
AUTOMATIC_PROMOTION = False
PRODUCTION_PROMOTION_BLOCKED = True

CDN = "https://data.binance.vision"
INTERVAL = "15m"
MONTHS = ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
START = pd.Timestamp("2026-03-01T00:00:00Z")
END = pd.Timestamp("2026-08-31T23:59:59Z")
WORKERS = 24
UA = "tst-mid-momentum-archived-oos/1.0"
OUT = pathlib.Path("validation/edges/mid-momentum-archived-only-oos-latest.json")

# Frozen from BINANCE_VISION_RECENT_ARCHIVED_ONLY_AUDIT run 2026-09-10.
TARGETS = [
    "ACXUSDT","HFTUSDT","ICXUSDT","PIVXUSDT","PYRUSDT","SCRTUSDT","STORJUSDT","VANRYUSDT","VICUSDT",
    "ALCXUSDT","ARDRUSDT","NFPUSDT","PONDUSDT","COSUSDT","DUSDT","HIGHUSDT","MBOXUSDT","TONUSDT",
    "ATAUSDT","FARMUSDT","MLNUSDT","PHBUSDT","SYSUSDT","A2ZUSDT","BIFIUSDT","DEGOUSDT","DENTUSDT",
    "FIOUSDT","FORTHUSDT","FUNUSDT","HOOKUSDT","IDEXUSDT","LRCUSDT","MDTUSDT","NTRNUSDT","OXTUSDT",
    "RDNTUSDT","SXPUSDT","TRUUSDT","UTKUSDT","WANUSDT",
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def month_url(symbol: str, month: str) -> str:
    return f"{CDN}/data/spot/monthly/klines/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{month}.zip"


def fetch_month(symbol: str, month: str):
    req = urllib.request.Request(month_url(symbol, month), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            return None
        text = zf.read(names[0]).decode("utf-8-sig", errors="replace")
    rows = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 11:
            continue
        try:
            ts0 = int(float(r[0]))
            # Spot Vision switched to microsecond timestamps in 2025.
            unit = "us" if ts0 > 100_000_000_000_000 else "ms"
            ts = pd.to_datetime(ts0, unit=unit, utc=True)
            rows.append({
                "ts": ts,
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
                "quote_volume": float(r[7]),
                "trades": int(float(r[8])),
                "taker_base": float(r[9]),
                "taker_quote": float(r[10]),
            })
        except Exception:
            continue
    return pd.DataFrame(rows) if rows else None


def vision_symbol(symbol: str) -> pd.DataFrame:
    parts = []
    present = []
    for month in MONTHS:
        z = fetch_month(symbol, month)
        if z is not None and not z.empty:
            parts.append(z)
            present.append(month)
    if not parts:
        raise RuntimeError("no Vision monthly bars")
    x = pd.concat(parts, ignore_index=True).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    x = x[(x.ts >= START) & (x.ts <= END)].copy()
    if len(x) < 96 * 20:
        raise RuntimeError(f"insufficient Vision bars {len(x)} months={present}")
    x["symbol"] = symbol
    x.attrs["monthsPresent"] = present
    return x


def current_reference(symbol: str) -> pd.DataFrame:
    x = base.klines(symbol).copy()
    x["ts"] = pd.to_datetime(x.ts, utc=True)
    x = x[(x.ts >= START) & (x.ts <= END)].copy().sort_values("ts").reset_index(drop=True)
    if len(x) < 96 * 20:
        raise RuntimeError(f"insufficient reference bars {len(x)}")
    return x


def pass_summary(s: dict) -> bool:
    return bool(s.get("n", 0) >= mid.MIN_VAL and s.get("meanNet", -1) > 0 and s.get("profitFactor", 0) >= 1.05)


def main() -> None:
    started = time.time()
    target_data = {}
    target_failures = {}
    target_months = {}

    log(f"target archived-only universe={len(TARGETS)}")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        fut = {ex.submit(vision_symbol, s): s for s in TARGETS}
        for n, f in enumerate(as_completed(fut), 1):
            s = fut[f]
            try:
                z = f.result()
                target_data[s] = z
                target_months[s] = list(z.attrs.get("monthsPresent", []))
                log(f"target {n}/{len(TARGETS)} {s} bars={len(z)} months={len(target_months[s])}")
            except Exception as exc:
                target_failures[s] = f"{type(exc).__name__}: {exc}"
                log(f"target {n}/{len(TARGETS)} {s} FAIL {exc}")

    if not target_data:
        raise RuntimeError("no archived-only target data loaded")

    # Current V1-like 70-symbol reference panel for cross-sectional ranks.
    reference_syms = base.universe()
    reference_data = {}
    reference_failures = {}
    log(f"reference universe={len(reference_syms)}")
    with ThreadPoolExecutor(max_workers=base.DOWNLOAD_WORKERS) as ex:
        fut = {ex.submit(current_reference, s): s for s in reference_syms}
        for n, f in enumerate(as_completed(fut), 1):
            s = fut[f]
            try:
                reference_data[s] = f.result()
                log(f"reference {n}/{len(reference_syms)} {s} bars={len(reference_data[s])}")
            except Exception as exc:
                reference_failures[s] = f"{type(exc).__name__}: {exc}"
                log(f"reference {n}/{len(reference_syms)} {s} FAIL {exc}")

    # Use BTC reference from current loader; fallback to Vision if absent.
    if "BTCUSDT" in reference_data:
        btc = reference_data["BTCUSDT"]
    else:
        btc = vision_symbol("BTCUSDT")

    frames = []
    for s, raw in reference_data.items():
        if s == "BTCUSDT" or s in target_data:
            continue
        try:
            frames.append(mid.augment(raw, btc))
        except Exception as exc:
            reference_failures[s] = f"augment:{type(exc).__name__}:{exc}"
    for s, raw in target_data.items():
        frames.append(mid.augment(raw, btc))

    p = mid.cross_sectional(pd.concat(frames, ignore_index=True))
    needed = ["ret_15m","ret_1h","ret_4h","ret_24h","qv24","volume_ratio","taker_buy_ratio","rsi14","btc_ret_4h","btc_ret_24h"]
    p = p.dropna(subset=needed).sort_values(["symbol","ts"]).reset_index(drop=True)
    target_panel = p[p.symbol.isin(target_data)].copy()

    # Raw lookup only for targets because only targets are scored as OOS events.
    raw = {s: z.sort_values("ts").reset_index(drop=True) for s, z in target_data.items()}
    lookup = {s: {pd.Timestamp(t): i for i, t in enumerate(g.ts)} for s, g in raw.items()}

    results = []
    for family, mask_all in mid.masks(p).items():
        target_mask = mask_all & p.symbol.isin(target_data)
        selected = mid.event_rows(p, target_mask)
        for cname, contract in mid.CONTRACTS.items():
            rows = []
            by_symbol = {}
            for r in selected.itertuples(index=False):
                idx = lookup.get(r.symbol, {}).get(pd.Timestamp(r.ts))
                if idx is None:
                    continue
                # Regime labels are informational only; recreate the same categories.
                if r.btc_ret_4h > 0.01:
                    regime = "BTC_UP"
                elif r.btc_ret_4h < -0.01:
                    regime = "BTC_DOWN"
                elif r.breadth_4h >= 0.60:
                    regime = "BROAD_UP"
                elif r.breadth_4h <= 0.40:
                    regime = "BROAD_DOWN"
                else:
                    regime = "MIXED"
                z = mid.barrier(raw[r.symbol], idx, contract, r.ts, regime)
                if z:
                    z["symbol"] = r.symbol
                    rows.append(z)
            for s in sorted({r["symbol"] for r in rows}):
                rs = [r for r in rows if r["symbol"] == s]
                by_symbol[s] = mid.summarize(rs, mid.COST_NORMAL)

            normal = mid.summarize(rows, mid.COST_NORMAL)
            stress = mid.summarize(rows, mid.COST_STRESS)
            boot = mid.bootstrap(rows, mid.COST_NORMAL)
            normal_pass = pass_summary(normal)
            stress_pass = pass_summary(stress)
            bootstrap_pass = boot.get("status") == "OK" and boot.get("p05", -1) > 0
            results.append({
                "family": family,
                "contract": cname,
                "normal": normal,
                "stress": stress,
                "bootstrap": boot,
                "normalPass": normal_pass,
                "stressPass": stress_pass,
                "bootstrapPass": bootstrap_pass,
                "oosReplicationPass": bool(normal_pass and stress_pass and bootstrap_pass),
                "eventSymbols": len(by_symbol),
                "bySymbol": by_symbol,
            })

    winners = [x for x in results if x["oosReplicationPass"]]
    ranked = sorted(results, key=lambda x: x["normal"].get("meanNet", -99), reverse=True)
    payload = {
        "engine": "MID_MOMENTUM_ARCHIVED_ONLY_OOS_V1",
        "authorization": AUTHORIZATION,
        "liveTrading": LIVE_TRADING,
        "automaticPromotion": AUTOMATIC_PROMOTION,
        "productionPromotionBlocked": PRODUCTION_PROMOTION_BLOCKED,
        "frozenFrom": "MID_MOMENTUM_CONTINUATION_V1",
        "targetDefinition": "41 archived-only USDT Spot symbols with >=2 recent monthly 15m archives in Mar-Aug 2026",
        "window": {"start": str(START), "end": str(END)},
        "symbolsRequested": TARGETS,
        "symbolsLoaded": sorted(target_data),
        "symbolMonths": target_months,
        "targetFailures": target_failures,
        "referenceSymbolsRequested": reference_syms,
        "referenceSymbolsLoaded": sorted(reference_data),
        "referenceFailures": reference_failures,
        "validationDesign": {
            "targetBars": "BINANCE_VISION_SPOT_MONTHLY_15M",
            "referencePanel": "CURRENT_V1_70_SYMBOL_PANEL_FOR_CROSS_SECTIONAL_RANKS",
            "entryTiming": "NEXT_15M_OPEN",
            "sameBarTpSlResolution": "SL_FIRST_PESSIMISTIC",
            "eventDeclusterHours": 4,
            "costNormal": mid.COST_NORMAL,
            "costStress": mid.COST_STRESS,
            "bootstrap": "day-cluster",
            "passGate": "n>=20 AND meanNet>0 AND PF>=1.05 at normal and stress costs AND bootstrap p05>0",
            "thresholdOptimization": False,
        },
        "hypothesisCount": len(results),
        "oosWinnerCount": len(winners),
        "oosWinners": winners,
        "rankedDiagnostics": ranked,
        "runtimeSeconds": round(time.time() - started, 2),
        "decision": "NO_LIVE_PROMOTION_REGARDLESS_OF_RESULT",
        "note": "This is an independent survivorship-bias sensitivity check. V1 had zero full-pipeline survivors, so an archived-only pass alone cannot justify live deployment.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    log(f"DONE hypotheses={len(results)} oos_winners={len(winners)} targets_loaded={len(target_data)}")


if __name__ == "__main__":
    main()
