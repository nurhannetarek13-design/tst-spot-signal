#!/usr/bin/env python3
"""Probe temporal/update-id alignment of Tardis raw Binance Futures channels.
Research-only diagnostic; never trades.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import urllib.parse

BASE = "https://api.tardis.dev/v1/data-feeds/binance-futures"


def fetch_combined(symbol: str, date: str, offset: int) -> str:
    filters = [
        {"channel": "depthSnapshot", "symbols": [symbol.lower()]},
        {"channel": "depth", "symbols": [symbol.lower()]},
        {"channel": "bookTicker", "symbols": [symbol.lower()]},
    ]
    encoded = urllib.parse.quote(json.dumps(filters, separators=(",", ":")), safe='[]{}\":,')
    url = f"{BASE}?from={urllib.parse.quote(date, safe=':-TZ')}&filters={encoded}&offset={offset}"
    p = subprocess.run(["curl", "--compressed", "-sS", "-g", url], capture_output=True, check=True)
    return p.stdout.decode("utf-8", "replace")


def parse(body: str) -> list[dict]:
    rows = []
    for n, raw in enumerate(body.splitlines()):
        raw = raw.strip()
        if not raw:
            continue
        i = raw.find("{")
        if i < 0:
            continue
        prefix = raw[:i].strip()
        try:
            wrapper = json.loads(raw[i:])
        except json.JSONDecodeError:
            continue
        stream = wrapper.get("stream") if isinstance(wrapper, dict) else None
        data = wrapper.get("data") if isinstance(wrapper, dict) and isinstance(wrapper.get("data"), dict) else wrapper
        if not isinstance(data, dict):
            continue
        rows.append({"line": n, "prefix": prefix, "stream": stream, "data": data})
    return rows


def compact(r: dict) -> dict:
    d = r["data"]
    out = {"line": r["line"], "prefix": r["prefix"], "stream": r["stream"]}
    for k in ("e", "E", "T", "s", "U", "u", "pu", "lastUpdateId", "b", "B", "a", "A"):
        if k in d:
            out[k] = d[k]
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", required=True)
    p.add_argument("--date", default="2021-09-01")
    p.add_argument("--offset", type=int, default=0)
    a = p.parse_args()

    rows = parse(fetch_combined(a.symbol, a.date, a.offset))
    snaps = [r for r in rows if "lastUpdateId" in r["data"] and "bids" in r["data"]]
    depths = [r for r in rows if {"U", "u"}.issubset(r["data"])]
    tickers = [r for r in rows if {"u", "b", "B", "a", "A"}.issubset(r["data"])]

    snapshot = snaps[0] if snaps else None
    sid = int(snapshot["data"]["lastUpdateId"]) if snapshot else None
    bridge = None
    before_bridge = []
    if sid is not None:
        wanted = sid + 1
        for r in depths:
            d = r["data"]
            U, u = int(d["U"]), int(d["u"])
            if len(before_bridge) < 5:
                before_bridge.append(compact(r))
            if U <= wanted <= u:
                bridge = compact(r)
                break

    result = {
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "symbol": a.symbol,
        "date": a.date,
        "offset": a.offset,
        "rowCount": len(rows),
        "snapshotCount": len(snaps),
        "depthCount": len(depths),
        "bookTickerCount": len(tickers),
        "snapshot": compact(snapshot) if snapshot else None,
        "firstDepth": compact(depths[0]) if depths else None,
        "firstBookTicker": compact(tickers[0]) if tickers else None,
        "bridge": bridge,
        "firstFiveDepth": [compact(x) for x in depths[:5]],
        "lastFiveDepth": [compact(x) for x in depths[-5:]],
        "firstFiveBookTicker": [compact(x) for x in tickers[:5]],
        "streamOrderFirst20": [compact(x) for x in rows[:20]],
    }
    if sid is not None and depths:
        result["snapshotToFirstDepthGap"] = int(depths[0]["data"]["U"]) - (sid + 1)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
