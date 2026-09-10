#!/usr/bin/env python3
"""Research-only Tardis incremental L2 state engine.

Input schema follows Tardis downloadable CSV incremental_book_L2:
exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount

Semantics:
- is_snapshot=true begins/replaces a book snapshot state.
- amount=0 deletes a price level.
- bid/ask updates replace the amount at that price.

Outputs top-of-book microprice, depth imbalance N, spread, mid and a simple
book-state OFI proxy from changes in aggregated bid/ask depth. No trading.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import pathlib
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable, TextIO

AUTHORIZATION = "RESEARCH_ONLY"
REQUIRED = ["exchange","symbol","timestamp","local_timestamp","is_snapshot","side","price","amount"]


def to_bool(v) -> bool:
    return str(v).strip().lower() in {"1","true","t","yes"}


@dataclass
class L2Book:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    snapshot_ts: int | None = None
    last_ts: int | None = None
    last_bid_depth: dict[int, float] = field(default_factory=dict)
    last_ask_depth: dict[int, float] = field(default_factory=dict)

    def clear(self) -> None:
        self.bids.clear(); self.asks.clear()
        self.last_bid_depth.clear(); self.last_ask_depth.clear()

    def apply(self, row: dict) -> None:
        ts = int(row["timestamp"])
        if to_bool(row["is_snapshot"]):
            # Tardis snapshot rows for the same snapshot timestamp arrive as a block.
            # Clear once when a new snapshot timestamp begins.
            if self.snapshot_ts != ts:
                self.clear()
                self.snapshot_ts = ts
        side = str(row["side"]).strip().lower()
        price = float(row["price"])
        amount = float(row["amount"])
        book = self.bids if side == "bid" else self.asks if side == "ask" else None
        if book is None:
            raise ValueError(f"unknown side: {side}")
        if amount <= 0:
            book.pop(price, None)
        else:
            book[price] = amount
        self.last_ts = ts

    def top(self, n: int):
        bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:n]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[:n]
        return bids, asks

    def features(self, n: int = 10) -> dict | None:
        bids, asks = self.top(n)
        if not bids or not asks:
            return None
        bp, bv = bids[0]; ap, av = asks[0]
        if not (bp > 0 and ap > 0 and bp < ap):
            return None
        mid = (bp + ap) / 2.0
        spread = ap - bp
        spread_bps = spread / mid * 10000.0
        denom = bv + av
        micro = ((ap * bv + bp * av) / denom) if denom > 0 else mid
        micro_dev_bps = (micro / mid - 1.0) * 10000.0
        bid_depth = sum(v for _, v in bids)
        ask_depth = sum(v for _, v in asks)
        depth_denom = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / depth_denom if depth_denom > 0 else 0.0
        prev_b = self.last_bid_depth.get(n)
        prev_a = self.last_ask_depth.get(n)
        ofi = None if prev_b is None or prev_a is None else (bid_depth - prev_b) - (ask_depth - prev_a)
        self.last_bid_depth[n] = bid_depth
        self.last_ask_depth[n] = ask_depth
        return {
            "timestamp": self.last_ts,
            "best_bid": bp,
            "best_bid_amount": bv,
            "best_ask": ap,
            "best_ask_amount": av,
            "mid": mid,
            "spread": spread,
            "spread_bps": spread_bps,
            "microprice": micro,
            "microprice_deviation_bps": micro_dev_bps,
            f"bid_depth_{n}": bid_depth,
            f"ask_depth_{n}": ask_depth,
            f"depth_imbalance_{n}": imbalance,
            f"ofi_depth_proxy_{n}": ofi,
        }


def open_csv_source(path_or_url: str):
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        req = urllib.request.Request(path_or_url, headers={"User-Agent":"tst-tardis-l2-adapter/1.0"})
        raw = urllib.request.urlopen(req, timeout=60)
        if path_or_url.endswith(".gz"):
            return io.TextIOWrapper(gzip.GzipFile(fileobj=raw), encoding="utf-8")
        return io.TextIOWrapper(raw, encoding="utf-8")
    p = pathlib.Path(path_or_url)
    if p.suffix == ".gz":
        return gzip.open(p, "rt", encoding="utf-8", newline="")
    return p.open("r", encoding="utf-8", newline="")


def iter_rows(f: TextIO) -> Iterable[dict]:
    r = csv.DictReader(f)
    missing = [c for c in REQUIRED if c not in (r.fieldnames or [])]
    if missing:
        raise RuntimeError(f"missing required Tardis columns: {missing}; got={r.fieldnames}")
    yield from r


def process(path_or_url: str, depth: int = 10, emit_every: int = 1000, max_rows: int | None = None):
    book = L2Book()
    out = []
    seen = 0
    emitted = 0
    with open_csv_source(path_or_url) as f:
        for row in iter_rows(f):
            seen += 1
            book.apply(row)
            if seen % emit_every == 0:
                feat = book.features(depth)
                if feat:
                    feat.update({"exchange":row["exchange"], "symbol":row["symbol"], "local_timestamp":int(row["local_timestamp"])})
                    out.append(feat); emitted += 1
            if max_rows and seen >= max_rows:
                break
    return out, {"rowsSeen":seen,"featuresEmitted":emitted,"depthLevels":depth}


def self_test() -> None:
    b = L2Book()
    rows = [
        {"timestamp":"1000000","is_snapshot":"true","side":"bid","price":"100","amount":"5"},
        {"timestamp":"1000000","is_snapshot":"true","side":"ask","price":"101","amount":"4"},
        {"timestamp":"1000001","is_snapshot":"false","side":"bid","price":"99","amount":"3"},
        {"timestamp":"1000002","is_snapshot":"false","side":"ask","price":"102","amount":"2"},
    ]
    for r in rows: b.apply(r)
    f = b.features(2)
    assert f is not None
    assert f["best_bid"] == 100 and f["best_ask"] == 101
    expected_micro = (101*5 + 100*4)/9
    assert abs(f["microprice"]-expected_micro) < 1e-12
    assert abs(f["depth_imbalance_2"] - ((8-6)/(8+6))) < 1e-12
    # deletion semantics
    b.apply({"timestamp":"1000003","is_snapshot":"false","side":"bid","price":"100","amount":"0"})
    f2=b.features(2); assert f2 and f2["best_bid"]==99
    # new snapshot must replace old state
    b.apply({"timestamp":"2000000","is_snapshot":"true","side":"bid","price":"200","amount":"1"})
    b.apply({"timestamp":"2000000","is_snapshot":"true","side":"ask","price":"201","amount":"1"})
    f3=b.features(1); assert f3 and f3["best_bid"]==200 and f3["best_ask"]==201
    print(json.dumps({"authorization":AUTHORIZATION,"liveTrading":False,"selfTest":"PASS","features":f3}, indent=2))


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--input")
    p.add_argument("--depth",type=int,default=10)
    p.add_argument("--emit-every",type=int,default=1000)
    p.add_argument("--max-rows",type=int)
    p.add_argument("--output")
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:
        self_test(); return
    if not a.input:
        raise SystemExit("--input required")
    rows, meta=process(a.input,a.depth,a.emit_every,a.max_rows)
    payload={"engine":"TARDIS_L2_STATE_ENGINE_V1","authorization":AUTHORIZATION,"liveTrading":False,"meta":meta,"rows":rows}
    text=json.dumps(payload,indent=2)
    if a.output: pathlib.Path(a.output).write_text(text,encoding="utf-8")
    print(json.dumps({"engine":payload["engine"],**meta},separators=(",",":")))

if __name__=="__main__":
    main()
