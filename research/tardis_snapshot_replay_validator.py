#!/usr/bin/env python3
"""Validate Binance USD-M L2 reconstruction against the next available Tardis depthSnapshot.
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
    p = subprocess.run(["curl", "--compressed", "-sS", "-g", url], capture_output=True, check=True)
    return p.stdout.decode("utf-8", "replace")


def parse(body: str) -> list[dict]:
    out = []
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


def snapshot_or_none(symbol: str, date: str, offset: int) -> dict | None:
    xs = [
        x for x in parse(fetch(symbol, date, offset, "depthSnapshot"))
        if "lastUpdateId" in x and "bids" in x and "asks" in x
    ]
    return xs[0] if xs else None


def require_snapshot(symbol: str, date: str, offset: int) -> dict:
    s = snapshot_or_none(symbol, date, offset)
    if s is None:
        raise RuntimeError(f"no depthSnapshot for offset={offset}")
    return s


def find_next_snapshot(symbol: str, date: str, start_offset: int, max_scan: int) -> tuple[int, dict]:
    for off in range(start_offset + 1, start_offset + max_scan + 1):
        s = snapshot_or_none(symbol, date, off)
        if s is not None:
            return off, s
    raise RuntimeError(
        f"no later depthSnapshot found in offsets {start_offset + 1}..{start_offset + max_scan}"
    )


def build_snapshot(s: dict) -> tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]:
    def side(rows):
        return {Decimal(str(p)): Decimal(str(q)) for p, q in rows if Decimal(str(q)) > 0}
    return side(s["bids"]), side(s["asks"])


def update_side(book: dict[Decimal, Decimal], rows) -> None:
    for p, q in rows:
        p, q = Decimal(str(p)), Decimal(str(q))
        if q == 0:
            book.pop(p, None)
        else:
            book[p] = q


def top(book: dict[Decimal, Decimal], side: str, n: int) -> list[list[str]]:
    prices = sorted(book, reverse=(side == "bid"))[:n]
    return [[str(p), str(book[p])] for p in prices]


def normalize_snapshot_top(s: dict, side: str, n: int) -> list[list[str]]:
    key = "bids" if side == "bid" else "asks"
    b = {Decimal(str(p)): Decimal(str(q)) for p, q in s[key] if Decimal(str(q)) > 0}
    return top(b, side, n)


def replay(symbol: str, date: str, offset: int, n: int, max_snapshot_scan: int) -> dict:
    s0 = require_snapshot(symbol, date, offset)
    target_offset, s1 = find_next_snapshot(symbol, date, offset, max_snapshot_scan)
    sid0, sid1 = int(s0["lastUpdateId"]), int(s1["lastUpdateId"])
    if sid1 <= sid0:
        raise RuntimeError(f"non-increasing snapshots: {sid0} -> {sid1}")

    events: list[dict] = []
    for off in range(offset, target_offset + 1):
        events.extend(depth_messages(symbol, date, off))

    bids, asks = build_snapshot(s0)
    started = False
    prev_u = None
    applied = 0
    first_bridge = None
    target_event = None
    continuity_checks = 0

    for e in events:
        U, u = int(e["U"]), int(e["u"])
        if u <= sid0:
            continue

        if not started:
            want = sid0 + 1
            if U <= want <= u:
                started = True
                first_bridge = {"U": U, "u": u, "pu": int(e["pu"]) if "pu" in e else None}
            elif U > want:
                return {"status": "FIRST_EVENT_GAP", "expected": want, "U": U, "u": u}
            else:
                continue
        else:
            if "pu" not in e:
                return {"status": "MISSING_PU", "U": U, "u": u}
            continuity_checks += 1
            if int(e["pu"]) != int(prev_u):
                return {
                    "status": "PU_GAP", "prev_u": prev_u,
                    "pu": int(e["pu"]), "U": U, "u": u,
                }

        if u > sid1:
            return {
                "status": "TARGET_INSIDE_EVENT",
                "targetSnapshotId": sid1,
                "event": {"U": U, "u": u},
                "eventsApplied": applied,
                "targetOffset": target_offset,
            }

        update_side(bids, e.get("b", []))
        update_side(asks, e.get("a", []))
        prev_u = u
        applied += 1

        if bids and asks and max(bids) >= min(asks):
            return {
                "status": "CROSSED_BOOK", "U": U, "u": u,
                "bestBid": str(max(bids)), "bestAsk": str(min(asks)),
            }

        if u == sid1:
            target_event = {"U": U, "u": u, "pu": int(e.get("pu", -1))}
            break

    if not started:
        return {"status": "NO_BRIDGE", "snapshotId": sid0, "targetOffset": target_offset}
    if target_event is None:
        return {
            "status": "TARGET_NOT_REACHED",
            "targetSnapshotId": sid1,
            "last_u": prev_u,
            "eventsApplied": applied,
            "targetOffset": target_offset,
        }

    replay_bids, replay_asks = top(bids, "bid", n), top(asks, "ask", n)
    snap_bids = normalize_snapshot_top(s1, "bid", n)
    snap_asks = normalize_snapshot_top(s1, "ask", n)
    bid_ok, ask_ok = replay_bids == snap_bids, replay_asks == snap_asks

    def first_diff(a, b):
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return {"index": i, "replayed": x, "snapshot": y}
        if len(a) != len(b):
            return {"index": min(len(a), len(b)), "replayedLength": len(a), "snapshotLength": len(b)}
        return None

    return {
        "status": "PASS" if bid_ok and ask_ok else "MISMATCH",
        "symbol": symbol,
        "date": date,
        "startOffset": offset,
        "targetOffset": target_offset,
        "snapshotGapMinutes": target_offset - offset,
        "startSnapshotId": sid0,
        "targetSnapshotId": sid1,
        "firstBridge": first_bridge,
        "targetEvent": target_event,
        "eventsApplied": applied,
        "continuityChecks": continuity_checks,
        "depthCompared": n,
        "bidExact": bid_ok,
        "askExact": ask_ok,
        "firstBidDiff": first_diff(replay_bids, snap_bids),
        "firstAskDiff": first_diff(replay_asks, snap_asks),
        "replayedTop": {"bids": replay_bids, "asks": replay_asks},
        "snapshotTop": {"bids": snap_bids, "asks": snap_asks},
        "canonicalReplayReady": bool(bid_ok and ask_ok),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTCUSDT", choices=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    p.add_argument("--date", default="2021-09-01")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--depth", type=int, default=20)
    p.add_argument("--max-snapshot-scan", type=int, default=60)
    a = p.parse_args()
    result = replay(a.symbol, a.date, a.offset, a.depth, a.max_snapshot_scan)
    out = {"authorization": AUTHORIZATION, "liveTrading": False, **result}
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
