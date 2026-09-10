#!/usr/bin/env python3
"""Research-only audit of archived-only USDT Spot symbols with recent 15m data.

This identifies symbols present in Binance Vision during the fixed Mar-Aug 2026
window but absent from the current trading universe. Those symbols are required
for a less survivorship-biased validation sample. No live trading changes.
"""
from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from research.binance_vision_archive_universe import archive_symbols, current_spot, tradable_research_symbol

CDN = "https://data.binance.vision"
MONTHS = ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
INTERVAL = "15m"
WORKERS = 32
OUT = pathlib.Path("validation/edges/binance-vision-recent-archived-only.json")
UA = "tst-recent-archived-only-audit/1.0"


def url(symbol: str, month: str) -> str:
    return f"{CDN}/data/spot/monthly/klines/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{month}.zip"


def exists(symbol: str, month: str) -> bool:
    req = urllib.request.Request(url(symbol, month), method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= int(r.status) < 400
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def check_symbol(symbol: str) -> tuple[str, list[str], list[str]]:
    yes, errors = [], []
    for month in MONTHS:
        try:
            if exists(symbol, month):
                yes.append(month)
        except Exception as exc:
            errors.append(f"{month}:{type(exc).__name__}:{exc}")
    return symbol, yes, errors


def main() -> None:
    archived = sorted(s for s in archive_symbols() if s.endswith("USDT") and tradable_research_symbol(s))
    current = current_spot()
    active = {s for s, meta in current.items() if meta.get("status") == "TRADING" and tradable_research_symbol(s)}
    archived_only = sorted(set(archived) - active)

    rows = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        fut = {ex.submit(check_symbol, s): s for s in archived_only}
        for f in as_completed(fut):
            s, months, errors = f.result()
            if months or errors:
                rows.append({"symbol": s, "monthsPresent": months, "monthsCount": len(months), "errors": errors})

    recent = sorted((r for r in rows if r["monthsCount"] > 0), key=lambda r: (-r["monthsCount"], r["symbol"]))
    usable2 = [r for r in recent if r["monthsCount"] >= 2]
    usable3 = [r for r in recent if r["monthsCount"] >= 3]
    payload = {
        "engine": "BINANCE_VISION_RECENT_ARCHIVED_ONLY_AUDIT",
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "interval": INTERVAL,
        "fixedMonths": MONTHS,
        "counts": {
            "archivedOnlyAllHistory": len(archived_only),
            "archivedOnlyWithAnyRecentMonth": len(recent),
            "archivedOnlyWithAtLeast2Months": len(usable2),
            "archivedOnlyWithAtLeast3Months": len(usable3),
        },
        "recentArchivedOnly": recent,
        "recommendedValidationIncrement": [r["symbol"] for r in usable2],
        "nextGate": "Re-run mid-momentum validation on current plus these archived-only symbols using archive bars and point-in-time liquidity; no live promotion from this audit.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"engine": payload["engine"], **payload["counts"]}, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
