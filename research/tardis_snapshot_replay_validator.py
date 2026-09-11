#!/usr/bin/env python3
"""Validate Binance USD-M L2 reconstruction from one Tardis initial snapshot.

Ground truth used here:
- depthSnapshot: initial REST snapshot exposed by Tardis raw feed
- depth: exchange-native incremental U/u/pu messages
- bookTicker: independent exchange-native best bid/ask stream

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


def fetch(symbol: str, date: str, offset: int, channel: str) -> str:
    filters = json.dumps([{"channel": channel, "symbols": [symbol.lower()]}], separators=(",", ":"))
    url = (
        f"{BASE}?from={urllib.parse.quote(date, safe=':-TZ')}"
        f"&filters={urllib.parse.quote(filters, safe='[]{}\":,')}&offset={offset}"
    )
    p = subprocess.run(
        ["curl", "--compressed", "-sS", "-g", url],
        capture_output=True,
        check=True,
    )
    return p.stdout.decode("utf-8", "replace")


def parse(body: str) -> list[dict]:
    out: list[dict] = []
    for raw in body.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        i = raw.find("{")
        if i < 0:
            continue
        try:
            obj = json.loads(raw[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("data"), dict):
            obj = obj["data"]
        if isinstance(obj, dict):
            out.append(obj)
    return out


def depth_messages(symbol: str, date: str, offset: int) -> list[dict]:
    return [
        x for x in parse(fetch(symbol, date, offset, "depth"))
        if x.get("e") == "depthUpdate" or {"U", "u"}.issubset(x)
    ]


def book_tickers(symbol: str, date: str, offset: int) -> list[dict]:
    xs = parse(fetch(symbol, date, offset, "bookTicker"))
    return [x for x in xs if {"u", "b", "B", "a", "A"}.issubset(x)]


def snapshot(symbol: str, date: str, offset: int) -> dict:
    xs = [
        x for x in parse(fetch(symbol, date, offset, "depthSnapshot"))
        if "lastUpdateId" in x and "bids" in x and "asks" in x
    ]
    if not xs:
        raise RuntimeError(f"no depthSnapshot for offset={offset}")
    return xs[0]


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


def as_pair(price: Decimal, qty: Decimal) -> list[str]:
    return [str(price), str(qty)]


def validate(symbol: str, date: str, offset: int) -> dict:
    s0 = snapshot(symbol, date, offset)
    sid0 = int(s0["lastUpdateId"])
    depth = depth_messages(symbol, date, offset)
    tickers = book_tickers(symbol, date, offset)

    bids, asks = build_snapshot(s0)
    started = False
    prev_u: int | None = None
    first_bridge = None
    events_applied = 0
    continuity_checks = 0
    replay_by_u: dict[int, dict] = {}

    for e in depth:
        U = int(e["U"])
        u = int(e["u"])
        if u <= sid0:
            continue

        if not started:
            wanted = sid0 + 1
            if U <= wanted <= u:
                started = True
                first_bridge = {
                    "U": U,
                    "u": u,
                    "pu": int(e["pu"]) if "pu" in e else None,
                }
            elif U > wanted:
                return {
                    "status": "FIRST_EVENT_GAP",
                    "symbol": symbol,
                    "startSnapshotId": sid0,
                    "expected": wanted,
                    "U": U,
                    "u": u,
                    "canonicalReplayReady": False,
                }
            else:
                continue
        else:
            if "pu" not in e:
                return {
                    "status": "MISSING_PU",
                    "symbol": symbol,
                    "U": U,
                    "u": u,
                    "canonicalReplayReady": False,
                }
            continuity_checks += 1
            if int(e["pu"]) != int(prev_u):
                return {
                    "status": "PU_GAP",
                    "symbol": symbol,
                    "prev_u": prev_u,
                    "pu": int(e["pu"]),
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
                "U": U,
                "u": u,
                "bestBid": str(bid_p),
                "bestAsk": str(ask_p),
                "eventsApplied": events_applied,
                "continuityChecks": continuity_checks,
                "canonicalReplayReady": False,
            }

        replay_by_u[u] = {
            "bid": as_pair(bid_p, bid_q),
            "ask": as_pair(ask_p, ask_q),
        }

    if not started:
        return {
            "status": "NO_BRIDGE",
            "symbol": symbol,
            "startSnapshotId": sid0,
            "depthRecords": len(depth),
            "canonicalReplayReady": False,
        }

    ticker_comparable = 0
    ticker_exact = 0
    first_mismatch = None
    ticker_ids_seen = set()

    for t in tickers:
        tu = int(t["u"])
        ticker_ids_seen.add(tu)
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
                "u": tu,
                "replayed": replay,
                "bookTicker": ticker_state,
            }

    ticker_mismatches = ticker_comparable - ticker_exact
    match_rate = (ticker_exact / ticker_comparable) if ticker_comparable else 0.0
    ready = (
        events_applied > 0
        and continuity_checks > 0
        and ticker_comparable > 0
        and ticker_mismatches == 0
    )

    return {
        "status": "PASS" if ready else "BOOK_TICKER_PARITY_FAIL",
        "symbol": symbol,
        "date": date,
        "offset": offset,
        "startSnapshotId": sid0,
        "firstBridge": first_bridge,
        "depthRecords": len(depth),
        "eventsApplied": events_applied,
        "continuityChecks": continuity_checks,
        "replayStates": len(replay_by_u),
        "tickerRecords": len(tickers),
        "tickerUniqueUpdateIds": len(ticker_ids_seen),
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
    out = {"authorization": AUTHORIZATION, "liveTrading": False, **result}
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
