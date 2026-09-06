#!/usr/bin/env python3
"""Research-only Binance USD-M liquidation forward collector.

Captures the public !forceOrder@arr stream, stores normalized events in DuckDB,
and periodically exports immutable Parquet day partitions plus a JSON health file.
This collector records what Binance publishes; it must not be described as a
complete reconstruction of every exchange liquidation event.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import pathlib
import random
import signal
import time
from dataclasses import dataclass, asdict

import duckdb
import websockets

WS_URL = os.getenv("BINANCE_FORCE_ORDER_WS", "wss://fstream.binance.com/ws/!forceOrder@arr")
SYMBOLS = {s.strip().upper() for s in os.getenv("LIQ_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip()}
DATA_DIR = pathlib.Path(os.getenv("LIQ_DATA_DIR", "data/forward-liquidations"))
DB_PATH = DATA_DIR / "liquidations.duckdb"
HEALTH_PATH = DATA_DIR / "health.json"
PARQUET_DIR = DATA_DIR / "parquet"
FLUSH_SECONDS = int(os.getenv("LIQ_FLUSH_SECONDS", "60"))
HEALTH_SECONDS = int(os.getenv("LIQ_HEALTH_SECONDS", "30"))
STALE_SECONDS = int(os.getenv("LIQ_STALE_SECONDS", "180"))
AUTHORIZATION = "RESEARCH_ONLY"

@dataclass
class Health:
    authorization: str = AUTHORIZATION
    liveTrading: bool = False
    connected: bool = False
    startedAt: str = ""
    lastConnectAt: str | None = None
    lastDisconnectAt: str | None = None
    lastMessageAt: str | None = None
    lastRelevantEventAt: str | None = None
    messagesSeen: int = 0
    relevantEvents: int = 0
    reconnects: int = 0
    parseErrors: int = 0
    storageErrors: int = 0
    lastError: str | None = None
    stale: bool = False
    symbols: tuple[str, ...] = ()

def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def utc_day_ms(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).date().isoformat()

def normalize_message(payload: dict, received_ms: int) -> dict | None:
    order = payload.get("o") or {}
    symbol = str(order.get("s") or "").upper()
    if symbol not in SYMBOLS:
        return None
    event_ms = int(payload.get("E") or order.get("T") or received_ms)
    trade_ms = int(order.get("T") or event_ms)
    side = str(order.get("S") or "").upper()
    price = float(order.get("ap") or order.get("p") or 0.0)
    qty = float(order.get("z") or order.get("q") or 0.0)
    if price <= 0 or qty <= 0 or side not in {"BUY", "SELL"}:
        return None
    liquidation_side = "SHORT" if side == "BUY" else "LONG"
    return {"event_ms": event_ms,"trade_ms": trade_ms,"received_ms": received_ms,"symbol": symbol,"order_side": side,"liquidation_side": liquidation_side,"price": price,"quantity": qty,"notional": price * qty,"status": str(order.get("X") or ""),"raw_order_type": str(order.get("o") or ""),"time_in_force": str(order.get("f") or ""),"day": utc_day_ms(trade_ms)}

class Store:
    def __init__(self, path: pathlib.Path):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PARQUET_DIR.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path))
        self.con.execute("""CREATE TABLE IF NOT EXISTS liquidations (event_ms BIGINT,trade_ms BIGINT,received_ms BIGINT,symbol VARCHAR,order_side VARCHAR,liquidation_side VARCHAR,price DOUBLE,quantity DOUBLE,notional DOUBLE,status VARCHAR,raw_order_type VARCHAR,time_in_force VARCHAR,day VARCHAR,ingested_at TIMESTAMP DEFAULT current_timestamp)""")
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_liq_symbol_trade ON liquidations(symbol, trade_ms)")
    def insert(self, row: dict) -> None:
        self.con.execute("INSERT INTO liquidations(event_ms,trade_ms,received_ms,symbol,order_side,liquidation_side,price,quantity,notional,status,raw_order_type,time_in_force,day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",[row[k] for k in ("event_ms","trade_ms","received_ms","symbol","order_side","liquidation_side","price","quantity","notional","status","raw_order_type","time_in_force","day")])
    def checkpoint(self) -> None:
        self.con.execute("CHECKPOINT")
    def export_days(self) -> None:
        days = [r[0] for r in self.con.execute("SELECT DISTINCT day FROM liquidations ORDER BY day").fetchall()]
        for day in days:
            out = PARQUET_DIR / f"day={day}"
            out.mkdir(parents=True, exist_ok=True)
            target = out / "liquidations.parquet"
            tmp = out / "liquidations.tmp.parquet"
            safe_day = day.replace("'", "''")
            safe_tmp = str(tmp).replace("'", "''")
            self.con.execute(f"COPY (SELECT * EXCLUDE(day, ingested_at) FROM liquidations WHERE day='{safe_day}' ORDER BY trade_ms, symbol) TO '{safe_tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            tmp.replace(target)

def write_health(h: Health) -> None:
    HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(h)
    payload["symbols"] = list(h.symbols)
    now_ms = int(time.time() * 1000)
    if h.lastMessageAt:
        try:
            last_ms = int(dt.datetime.fromisoformat(h.lastMessageAt).timestamp() * 1000)
            payload["stale"] = h.connected and (now_ms - last_ms > STALE_SECONDS * 1000)
        except Exception:
            payload["stale"] = False
    tmp = HEALTH_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(HEALTH_PATH)

async def run_collector(stop: asyncio.Event) -> None:
    store = Store(DB_PATH)
    health = Health(startedAt=iso_now(), symbols=tuple(sorted(SYMBOLS)))
    last_flush = time.monotonic(); last_health = 0.0; backoff = 1.0
    while not stop.is_set():
        try:
            async with websockets.connect(WS_URL,ping_interval=20,ping_timeout=20,close_timeout=10,max_queue=4096) as ws:
                health.connected = True; health.lastConnectAt = iso_now(); health.lastError = None; write_health(health); backoff = 1.0
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(HEALTH_SECONDS, 60))
                    received_ms = int(time.time() * 1000); health.messagesSeen += 1; health.lastMessageAt = iso_now()
                    try:
                        payload = json.loads(raw); row = normalize_message(payload, received_ms)
                        if row:
                            store.insert(row); health.relevantEvents += 1; health.lastRelevantEventAt = iso_now()
                    except Exception as exc:
                        health.parseErrors += 1; health.lastError = f"parse: {type(exc).__name__}: {exc}"
                    now = time.monotonic()
                    if now - last_flush >= FLUSH_SECONDS:
                        try:
                            store.checkpoint(); store.export_days()
                        except Exception as exc:
                            health.storageErrors += 1; health.lastError = f"storage: {type(exc).__name__}: {exc}"
                        last_flush = now
                    if now - last_health >= HEALTH_SECONDS:
                        write_health(health); last_health = now
        except (asyncio.CancelledError, KeyboardInterrupt):
            break
        except Exception as exc:
            health.connected = False; health.lastDisconnectAt = iso_now(); health.reconnects += 1; health.lastError = f"socket: {type(exc).__name__}: {exc}"; write_health(health)
            await asyncio.sleep(backoff + random.random()); backoff = min(backoff * 2, 60.0)
    health.connected = False; health.lastDisconnectAt = iso_now()
    try:
        store.checkpoint(); store.export_days()
    except Exception as exc:
        health.storageErrors += 1; health.lastError = f"shutdown-storage: {type(exc).__name__}: {exc}"
    write_health(health)

def self_test() -> None:
    sample = {"e":"forceOrder","E":1704067200123,"o":{"s":"BTCUSDT","S":"SELL","o":"LIMIT","f":"IOC","q":"2.5","p":"42000","ap":"41990","X":"FILLED","T":1704067200000,"z":"2.0"}}
    row = normalize_message(sample, 1704067200200)
    assert row and row["symbol"] == "BTCUSDT" and row["liquidation_side"] == "LONG" and row["quantity"] == 2.0 and abs(row["notional"] - 83980.0) < 1e-9
    print(json.dumps({"authorization": AUTHORIZATION,"liveTrading": False,"selfTest":"PASS","row":row}, indent=2))

def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--self-test", action="store_true"); args = parser.parse_args()
    if args.self_test:
        self_test(); return
    stop = asyncio.Event(); loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, stop.set)
        except NotImplementedError: pass
    loop.run_until_complete(run_collector(stop))

if __name__ == "__main__":
    main()
