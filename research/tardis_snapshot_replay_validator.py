#!/usr/bin/env python3
"""Validate Binance USD-M L2 reconstruction from ordered Tardis raw data.

Canonical invariants:
- Fetch depthSnapshot + depth + bookTicker in ONE raw request so capture order is preserved.
- Start from the snapshot state.
- The first depth event after the snapshot must link with pu == snapshot.lastUpdateId.
- Every later depth event must satisfy pu == previous depth u.
- Reconstructed book must never cross.
- Where bookTicker and depth share the same update id, best bid/ask price+qty must match exactly.

Research-only. No trading actions.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import urllib.parse
from decimal import Decimal

BASE = "https://api.tardis.dev/v1/data-feeds/binance-futures"
AUTHORIZATION = "RESEARCH_ONLY"


def fetch_combined(symbol: str, date: str, offset: int) -> str:
    filters = [
        {"channel": "depthSnapshot", "symbols": [symbol.lower()]},
        {"channel": "depth", "symbols": [symbol.lower()]},
        {"channel": "bookTicker", "symbols": [symbol.lower()]},
    ]
    encoded = urllib.parse.quote(
        json.dumps(filters, separators=(",", ":")), safe='[]{}\":,'
    )
    url = (
        f"{BASE}?from={urllib.parse.quote(date, safe=':-TZ')}"
        f"&filters={encoded}&offset={offset}"
    )
    p = subprocess.run(
        ["curl", "--compressed", "-sS", "-g", url],
        capture_output=True,
        check=True,
    )
    return p.stdout.decode("utf-8", "replace")


def parse_ordered(body: str) -> list[dict]:
    out: list[dict] = []
    for line_no, raw in enumerate(body.splitlines()):
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
        data = (
            wrapper.get("data")
            if isinstance(wrapper, dict) and isinstance(wrapper.get("data"), dict)
            else wrapper
        )
        if isinstance(data, dict):
            out.append(
                {
                    "line": line_no,
                    "prefix": prefix,
                    "stream": stream,
                    "data": data,
                }
            )
    return out


def build_snapshot(s: dict) -> tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]:
    def side(rows):
        return {
            Decimal(str(p)): Decimal(str(q))
            for p, q in rows
            if Decimal(str(q)) > 0
        }
    return side(s["bids"]), side(s["asks"])


def update_side(book: dict[Decimal, Decimal], rows) -> None:
    for p, q in rows:
        price = Decimal(str(p))
        qty = Decimal(str(q))
        if qty == 0:
            book.pop(price, None)
        else:
            book[price] = qty


def best(book: dict[Decimal, Decimal], side: str) -> tuple[Decimal, Decimal]:
    if not book:
        raise RuntimeError(f"empty {side} book")
    price = max(book) if side == "bid" else min(book)
    return price, book[price]


def pair(price: Decimal, qty: Decimal) -> list[str]:
    return [str(price), str(qty)]


def validate(symbol: str, date: str, offset: int) -> dict:
    rows = parse_ordered(fetch_combined(symbol, date, offset))
    snapshots = [
        r for r in rows
        if "lastUpdateId" in r["data"] and "bids" in r["data"] and "asks" in r["data"]
    ]
    if not snapshots:
        return {
            "status": "NO_SNAPSHOT",
            "symbol": symbol,
            "canonicalReplayReady": False,
        }

    snap_row = snapshots[0]
    snap_line = int(snap_row["line"])
    s0 = snap_row["data"]
    sid0 = int(s0["lastUpdateId"])

    depth_rows = [
        r for r in rows
        if r["line"] > snap_line and {"U", "u", "pu"}.issubset(r["data"])
    ]
    ticker_rows = [
        r for r in rows
        if r["line"] > snap_line and {"u", "b", "B", "a", "A"}.issubset(r["data"])
    ]

    if not depth_rows:
        return {
            "status": "NO_DEPTH_AFTER_SNAPSHOT",
            "symbol": symbol,
            "startSnapshotId": sid0,
            "snapshotLine": snap_line,
            "canonicalReplayReady": False,
        }

    first = depth_rows[0]["data"]
    first_pu = int(first["pu"])
    if first_pu != sid0:
        return {
            "status": "SNAPSHOT_PU_BRIDGE_FAIL",
            "symbol": symbol,
            "startSnapshotId": sid0,
            "snapshotLine": snap_line,
            "firstDepthLine": depth_rows[0]["line"],
            "firstDepth": {
                "U": int(first["U"]),
                "u": int(first["u"]),
                "pu": first_pu,
            },
            "canonicalReplayReady": False,
        }

    bids, asks = build_snapshot(s0)
    prev_u = sid0
    events_applied = 0
    continuity_checks = 0
    replay_by_u: dict[int, dict] = {}
    first_bridge = {
        "snapshotId": sid0,
        "U": int(first["U"]),
        "u": int(first["u"]),
        "pu": first_pu,
        "snapshotLine": snap_line,
        "depthLine": depth_rows[0]["line"],
    }

    for r in depth_rows:
        e = r["data"]
        U, u, pu = int(e["U"]), int(e["u"]), int(e["pu"])
        continuity_checks += 1
        if pu != prev_u:
            return {
                "status": "PU_GAP",
                "symbol": symbol,
                "line": r["line"],
                "prev_u": prev_u,
                "pu": pu,
                "U": U,
                "u": u,
                "eventsApplied": events_applied,
                "continuityChecks": continuity_checks,
                "canonicalReplayReady": False,
            }

        update_side(bids, e.get("b", []))
        update_side(asks, e.get("a", []))
        prev_u = u
        events_applied += 1

        bid_p, bid_q = best(bids, "bid")
        ask_p, ask_q = best(asks, "ask")
        if bid_p >= ask_p:
            return {
                "status": "CROSSED_BOOK",
                "symbol": symbol,
                "line": r["line"],
                "U": U,
                "u": u,
                "bestBid": str(bid_p),
                "bestAsk": str(ask_p),
                "eventsApplied": events_applied,
                "continuityChecks": continuity_checks,
                "canonicalReplayReady": False,
            }

        replay_by_u[u] = {
            "bid": pair(bid_p, bid_q),
            "ask": pair(ask_p, ask_q),
        }

    ticker_comparable = 0
    ticker_exact = 0
    first_mismatch = None
    for r in ticker_rows:
        t = r["data"]
        tu = int(t["u"])
        replay = replay_by_u.get(tu)
        if replay is None:
            continue
        ticker_comparable += 1
        ticker_state = {
            "bid": [str(Decimal(str(t["b"]))), str(Decimal(str(t["B"])))],
            "ask": [str(Decimal(str(t["a"]))), str(Decimal(str(t["A"])))],
        }
        if replay == ticker_state:
            ticker_exact += 1
        elif first_mismatch is None:
            first_mismatch = {
                "line": r["line"],
                "u": tu,
                "replayed": replay,
                "bookTicker": ticker_state,
            }

    ticker_mismatches = ticker_comparable - ticker_exact
    match_rate = ticker_exact / ticker_comparable if ticker_comparable else 0.0
    ready = (
        events_applied > 0
        and continuity_checks == events_applied
        and ticker_comparable > 0
        and ticker_mismatches == 0
    )

    return {
        "status": "PASS" if ready else "BOOK_TICKER_PARITY_FAIL",
        "symbol": symbol,
        "date": date,
        "offset": offset,
        "rawRows": len(rows),
        "snapshotLine": snap_line,
        "snapshotPrefix": snap_row["prefix"],
        "startSnapshotId": sid0,
        "firstBridge": first_bridge,
        "depthAfterSnapshot": len(depth_rows),
        "eventsApplied": events_applied,
        "continuityChecks": continuity_checks,
        "finalDepthU": prev_u,
        "tickerAfterSnapshot": len(ticker_rows),
        "tickerComparable": ticker_comparable,
        "tickerExact": ticker_exact,
        "tickerMismatches": ticker_mismatches,
        "matchRate": match_rate,
        "firstMismatch": first_mismatch,
        "canonicalReplayReady": ready,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTCUSDT", choices=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    p.add_argument("--date", default="2021-09-01")
    p.add_argument("--offset", type=int, default=0)
    a = p.parse_args()
    result = validate(a.symbol, a.date, a.offset)
    print(json.dumps({"authorization": AUTHORIZATION, "liveTrading": False, **result}, indent=2))


if __name__ == "__main__":
    main()
