"""Pure Binance USD-M L2 replay engine for ordered Tardis raw rows.

No network access and no trading actions live here. The engine consumes already
parsed ordered raw rows, anchors on depthSnapshot, verifies Binance Futures
`pu` continuity, reconstructs L2 state, rejects crossed books, and checks
best-bid/ask parity against bookTicker wherever update ids overlap.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


def parse_ordered_raw(body: str) -> list[dict[str, Any]]:
    """Parse Tardis timestamp-prefixed raw lines while preserving line order."""
    out: list[dict[str, Any]] = []
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
            out.append({"line": line_no, "prefix": prefix, "stream": stream, "data": data})
    return out


def _build_snapshot(s: dict[str, Any]) -> tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]:
    def side(rows: list[list[str]]) -> dict[Decimal, Decimal]:
        return {Decimal(str(p)): Decimal(str(q)) for p, q in rows if Decimal(str(q)) > 0}
    return side(s["bids"]), side(s["asks"])


def _update_side(book: dict[Decimal, Decimal], rows: list[list[str]]) -> None:
    for p, q in rows:
        price = Decimal(str(p))
        qty = Decimal(str(q))
        if qty == 0:
            book.pop(price, None)
        else:
            book[price] = qty


def _best(book: dict[Decimal, Decimal], side: str) -> tuple[Decimal, Decimal]:
    if not book:
        raise RuntimeError(f"empty {side} book")
    price = max(book) if side == "bid" else min(book)
    return price, book[price]


def _pair(price: Decimal, qty: Decimal) -> list[str]:
    return [str(price), str(qty)]


def replay_ordered_rows(
    rows: list[dict[str, Any]],
    *,
    symbol: str,
    date: str | None = None,
    offset: int | None = None,
    max_ticker_mismatches: int = 0,
    min_ticker_match_rate: float = 1.0,
) -> dict[str, Any]:
    """Replay ordered raw rows and return a structured validation result.

    Defaults preserve exact bookTicker parity. Multi-minute research replays may
    explicitly allow a tiny diagnostic tolerance while still requiring an exact
    `pu` chain and a non-crossed reconstructed book.
    """
    snapshots = [r for r in rows if "lastUpdateId" in r["data"] and "bids" in r["data"] and "asks" in r["data"]]
    if not snapshots:
        return {"status": "NO_SNAPSHOT", "symbol": symbol, "canonicalReplayReady": False}

    snap_row = snapshots[0]
    snap_line = int(snap_row["line"])
    s0 = snap_row["data"]
    sid0 = int(s0["lastUpdateId"])

    depth_rows = [r for r in rows if r["line"] > snap_line and {"U", "u", "pu"}.issubset(r["data"])]
    ticker_rows = [r for r in rows if r["line"] > snap_line and {"u", "b", "B", "a", "A"}.issubset(r["data"])]

    if not depth_rows:
        return {"status": "NO_DEPTH_AFTER_SNAPSHOT", "symbol": symbol, "startSnapshotId": sid0, "snapshotLine": snap_line, "canonicalReplayReady": False}

    first = depth_rows[0]["data"]
    first_pu = int(first["pu"])
    if first_pu != sid0:
        return {
            "status": "SNAPSHOT_PU_BRIDGE_FAIL", "symbol": symbol, "startSnapshotId": sid0,
            "snapshotLine": snap_line, "firstDepthLine": depth_rows[0]["line"],
            "firstDepth": {"U": int(first["U"]), "u": int(first["u"]), "pu": first_pu},
            "canonicalReplayReady": False,
        }

    bids, asks = _build_snapshot(s0)
    prev_u = sid0
    events_applied = 0
    continuity_checks = 0
    replay_by_u: dict[int, dict[str, list[str]]] = {}
    first_bridge = {
        "snapshotId": sid0, "U": int(first["U"]), "u": int(first["u"]), "pu": first_pu,
        "snapshotLine": snap_line, "depthLine": depth_rows[0]["line"],
    }

    for r in depth_rows:
        e = r["data"]
        U, u, pu = int(e["U"]), int(e["u"]), int(e["pu"])
        continuity_checks += 1
        if pu != prev_u:
            return {
                "status": "PU_GAP", "symbol": symbol, "line": r["line"], "prev_u": prev_u,
                "pu": pu, "U": U, "u": u, "eventsApplied": events_applied,
                "continuityChecks": continuity_checks, "canonicalReplayReady": False,
            }
        _update_side(bids, e.get("b", []))
        _update_side(asks, e.get("a", []))
        prev_u = u
        events_applied += 1
        bid_p, bid_q = _best(bids, "bid")
        ask_p, ask_q = _best(asks, "ask")
        if bid_p >= ask_p:
            return {
                "status": "CROSSED_BOOK", "symbol": symbol, "line": r["line"], "U": U, "u": u,
                "bestBid": str(bid_p), "bestAsk": str(ask_p), "eventsApplied": events_applied,
                "continuityChecks": continuity_checks, "canonicalReplayReady": False,
            }
        replay_by_u[u] = {"bid": _pair(bid_p, bid_q), "ask": _pair(ask_p, ask_q)}

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
            first_mismatch = {"line": r["line"], "u": tu, "replayed": replay, "bookTicker": ticker_state}

    ticker_mismatches = ticker_comparable - ticker_exact
    match_rate = ticker_exact / ticker_comparable if ticker_comparable else 0.0
    depth_integrity = events_applied > 0 and continuity_checks == events_applied
    ticker_integrity = (
        ticker_comparable > 0
        and ticker_mismatches <= max_ticker_mismatches
        and match_rate >= min_ticker_match_rate
    )
    ready = depth_integrity and ticker_integrity

    result: dict[str, Any] = {
        "status": "PASS" if ready else "BOOK_TICKER_PARITY_FAIL",
        "symbol": symbol,
        "rawRows": len(rows),
        "snapshotLine": snap_line,
        "snapshotPrefix": snap_row["prefix"],
        "startSnapshotId": sid0,
        "firstBridge": first_bridge,
        "depthAfterSnapshot": len(depth_rows),
        "eventsApplied": events_applied,
        "continuityChecks": continuity_checks,
        "depthIntegrity": depth_integrity,
        "finalDepthU": prev_u,
        "tickerAfterSnapshot": len(ticker_rows),
        "tickerComparable": ticker_comparable,
        "tickerExact": ticker_exact,
        "tickerMismatches": ticker_mismatches,
        "matchRate": match_rate,
        "tickerIntegrity": ticker_integrity,
        "parityPolicy": {"maxTickerMismatches": max_ticker_mismatches, "minTickerMatchRate": min_ticker_match_rate},
        "firstMismatch": first_mismatch,
        "canonicalReplayReady": ready,
    }
    if date is not None:
        result["date"] = date
    if offset is not None:
        result["offset"] = offset
    return result
