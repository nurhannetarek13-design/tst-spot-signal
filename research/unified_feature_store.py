#!/usr/bin/env python3
"""Unified feature-store contract for TST research/shadow/live parity.

Pure transformations only: no orders, no account access, no secrets. The same
schema is intended to be consumed by historical research and shadow scoring.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import json, math, pathlib
from typing import Iterable

SCHEMA_VERSION = "tst-feature-store-v1"
REQUIRED = [
    "symbol","ts","price","ret_15m","ret_1h","volume_z","taker_buy_ratio",
    "flow_imbalance","open_interest","oi_change_1h","funding_rate","basis_bps",
    "btc_ret_1h","btc_volatility","relative_strength_1h","spread_bps",
]

@dataclass(frozen=True)
class FeatureRow:
    symbol: str
    ts: str
    price: float
    ret_15m: float
    ret_1h: float
    volume_z: float
    taker_buy_ratio: float
    flow_imbalance: float
    open_interest: float
    oi_change_1h: float
    funding_rate: float
    basis_bps: float
    btc_ret_1h: float
    btc_volatility: float
    relative_strength_1h: float
    spread_bps: float

    def validate(self) -> None:
        if not self.symbol.endswith("USDT"):
            raise ValueError("symbol must be USDT spot pair")
        if not self.ts:
            raise ValueError("timestamp required")
        for k, v in asdict(self).items():
            if k in {"symbol","ts"}: continue
            if not math.isfinite(float(v)):
                raise ValueError(f"non-finite feature: {k}")
        if self.price <= 0 or self.open_interest < 0 or self.spread_bps < 0:
            raise ValueError("invalid market-state value")
        if not 0 <= self.taker_buy_ratio <= 1:
            raise ValueError("taker_buy_ratio outside [0,1]")
        if not -1 <= self.flow_imbalance <= 1:
            raise ValueError("flow_imbalance outside [-1,1]")


def normalize(row: dict) -> FeatureRow:
    miss = [k for k in REQUIRED if k not in row]
    if miss: raise ValueError(f"missing features: {miss}")
    r = FeatureRow(**{k: row[k] for k in REQUIRED})
    r.validate(); return r


def write_jsonl(rows: Iterable[FeatureRow], path: str | pathlib.Path) -> int:
    p = pathlib.Path(path); p.parent.mkdir(parents=True, exist_ok=True); n = 0
    with p.open("w", encoding="utf-8") as f:
        for row in rows:
            row.validate(); f.write(json.dumps({"schema":SCHEMA_VERSION, **asdict(row)}, separators=(",",":")) + "\n"); n += 1
    return n


def selftest() -> None:
    r = normalize({"symbol":"SOLUSDT","ts":"2026-09-10T16:00:00Z","price":220.0,"ret_15m":0.002,"ret_1h":0.006,"volume_z":1.7,"taker_buy_ratio":0.59,"flow_imbalance":0.18,"open_interest":1000000.0,"oi_change_1h":0.012,"funding_rate":0.0001,"basis_bps":3.2,"btc_ret_1h":0.003,"btc_volatility":0.42,"relative_strength_1h":0.003,"spread_bps":1.8})
    assert normalize(asdict(r)) == r
    print(json.dumps({"schema":SCHEMA_VERSION,"selftest":"PASS"}))

if __name__ == "__main__": selftest()
