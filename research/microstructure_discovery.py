#!/usr/bin/env python3
"""Lean research-only microstructure diagnostic for Binance USD-M.

Uses Tardis free first-day-of-month liquidation samples plus Binance Vision USD-M
1m klines for taker-buy flow and forward returns. Phase 1 intentionally omits L2.
The Tardis PERPETUALS liquidation file is downloaded once per month and filtered
locally for BTCUSDT/ETHUSDT/SOLUSDT.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import statistics
from collections import defaultdict

from binance_vision_archive import load_klines
from tardis_sample_loader import _download_csv_gz, sample_url

OUT = pathlib.Path("validation/microstructure/tardis-discovery.json")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
SYMBOL_SET = set(SYMBOLS)
MONTHS = [(2024,1), (2024,2), (2024,3), (2024,4), (2024,5), (2024,6)]
MIN_MS = 60_000
HOUR_MS = 60 * MIN_MS


def f(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def minute_ms_from_us(value) -> int:
    return (int(value) // 1000 // MIN_MS) * MIN_MS


def summarize(rs):
    if not rs:
        return {"n": 0}
    fw = [r["fwd1h"] for r in rs]
    return {
        "n": len(rs),
        "meanFwd1hPct": round(100 * sum(fw) / len(fw), 5),
        "medianFwd1hPct": round(100 * statistics.median(fw), 5),
        "hitRatePct": round(100 * sum(x > 0 for x in fw) / len(fw), 2),
        "medianAggBuyShare": round(statistics.median(r["aggrBuyShare"] for r in rs), 4),
        "medianLiqNotional": round(statistics.median(r["liqNotional"] for r in rs), 2),
    }


records = []
failures = {}
data_counts = {}

for y, mo in MONTHS:
    liq_by_symbol = {s: defaultdict(lambda: {"liqNotional": 0.0, "buyLiq": 0.0, "sellLiq": 0.0, "liqCount": 0}) for s in SYMBOLS}
    liq_event_counts = {s: 0 for s in SYMBOLS}
    try:
        rows = _download_csv_gz(sample_url("liquidations", y, mo, "PERPETUALS"))
        for r in rows:
            symbol = (r.get("symbol") or "").upper()
            if symbol not in SYMBOL_SET:
                continue
            ts_us = r.get("timestamp") or r.get("local_timestamp")
            if not ts_us:
                continue
            m = minute_ms_from_us(ts_us)
            px = f(r.get("price"))
            qty = f(r.get("amount") or r.get("quantity"))
            n = px * qty
            side = (r.get("side") or "").lower()
            a = liq_by_symbol[symbol][m]
            a["liqCount"] += 1
            a["liqNotional"] += n
            liq_event_counts[symbol] += 1
            if side == "buy":
                a["buyLiq"] += n
            elif side == "sell":
                a["sellLiq"] += n
    except Exception as e:
        failures[f"liquidations-{y}-{mo:02d}"] = f"{type(e).__name__}: {e}"
        continue

    day = dt.datetime(y, mo, 1, tzinfo=dt.timezone.utc)
    start_ms = int(day.timestamp() * 1000)
    end_ms = start_ms + 24 * HOUR_MS
    price_end_ms = end_ms + HOUR_MS

    for symbol in SYMBOLS:
        key = f"{symbol}-{y}-{mo:02d}"
        try:
            liqs = liq_by_symbol[symbol]
            klines = load_klines("um", symbol, "1m", start_ms, price_end_ms)
            km = {int(r[0]): r for r in klines}
            day_minutes = sorted(ts for ts in km if start_ms <= ts < end_ms)
            data_counts[key] = {
                "minutes": len(day_minutes),
                "liquidationMinutes": len(liqs),
                "liquidationEvents": liq_event_counts[symbol],
            }

            for ts in day_minutes:
                row = km[ts]
                fut = km.get(ts + HOUR_MS)
                if fut is None:
                    continue
                quote_vol = f(row[7])
                taker_buy_quote = f(row[10])
                if quote_vol <= 0:
                    continue
                entry = f(row[4])
                future = f(fut[4])
                if entry <= 0 or future <= 0:
                    continue
                l = liqs.get(ts, {"liqNotional": 0.0, "buyLiq": 0.0, "sellLiq": 0.0, "liqCount": 0})
                total_liq = l["buyLiq"] + l["sellLiq"]
                records.append({
                    "symbol": symbol,
                    "month": f"{y}-{mo:02d}",
                    "minute": ts,
                    "aggrBuyShare": min(1.0, max(0.0, taker_buy_quote / quote_vol)),
                    "liqNotional": l["liqNotional"],
                    "liqCount": l["liqCount"],
                    "liqSellShare": l["sellLiq"] / total_liq if total_liq > 0 else None,
                    "fwd1h": future / entry - 1,
                })
        except Exception as e:
            failures[key] = f"{type(e).__name__}: {e}"

liq_values = [r["liqNotional"] for r in records if r["liqNotional"] > 0]
liq_cut = statistics.median(liq_values) if liq_values else math.inf
high = [r for r in records if r["liqNotional"] > 0 and r["liqNotional"] >= liq_cut]
zero = [r for r in records if r["liqNotional"] == 0]
sell_liq_buy_flow = [r for r in high if (r["liqSellShare"] or 0) >= 0.6 and r["aggrBuyShare"] >= 0.55]
buy_liq_buy_flow = [r for r in high if (r["liqSellShare"] or 0) <= 0.4 and r["aggrBuyShare"] >= 0.55]
sell_liq_sell_flow = [r for r in high if (r["liqSellShare"] or 0) >= 0.6 and r["aggrBuyShare"] <= 0.45]
buy_liq_sell_flow = [r for r in high if (r["liqSellShare"] or 0) <= 0.4 and r["aggrBuyShare"] <= 0.45]

report = {
    "schemaVersion": 3,
    "authorization": "RESEARCH_ONLY",
    "liveTrading": False,
    "sampleDesign": "TARDIS_FREE_FIRST_DAY_OF_MONTH_LIQUIDATIONS_PLUS_BINANCE_VISION_UM_1M",
    "phase": "LEAN_SIGNAL_SEPARATION",
    "months": [f"{y}-{m:02d}" for y, m in MONTHS],
    "symbols": SYMBOLS,
    "recordCount": len(records),
    "liquidationMedianNotionalCut": None if math.isinf(liq_cut) else liq_cut,
    "groups": {
        "highLiquidation": summarize(high),
        "noLiquidation": summarize(zero),
        "sellLiquidationPlusAggressiveBuy": summarize(sell_liq_buy_flow),
        "buyLiquidationPlusAggressiveBuy": summarize(buy_liq_buy_flow),
        "sellLiquidationPlusAggressiveSell": summarize(sell_liq_sell_flow),
        "buyLiquidationPlusAggressiveSell": summarize(buy_liq_sell_flow),
    },
    "dataCounts": data_counts,
    "failures": failures,
    "decisionRule": "Discovery only. Promote nothing from sparse first-day samples. Require withheld-month replication and long-window Binance Vision validation. Only run expensive L2 phase if directional separation is material and consistent.",
    "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
