#!/usr/bin/env python3
"""Research-only Binance USD-M all-market liquidation forward collector.

The collector persists only public liquidation snapshots published by Binance.
It is not a complete reconstruction of exchange liquidations and never enables
or touches live trading.
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
from dataclasses import asdict, dataclass

import duckdb
import websockets

AUTHORIZATION = "RESEARCH_ONLY"
STREAM_NAME = "!forceOrder@arr"
WS_URL = os.getenv("BINANCE_FORCE_ORDER_WS", "wss://fstream.binance.com/market/ws/!forceOrder@arr")
# LIQ_SYMBOLS remains the research universe used by the Coinalyze scripts.
# LIQ_CAPTURE_SCOPE controls only this forward collector so all-market capture
# cannot accidentally mutate historical-research symbol selection.
RESEARCH_SYMBOL_SPEC = os.getenv("LIQ_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").strip()
CAPTURE_SCOPE = os.getenv("LIQ_CAPTURE_SCOPE", "").strip().upper()
CAPTURE_ALL_USDT = CAPTURE_SCOPE in {"ALL", "ALL_USDT", "*"}
SYMBOLS = {s.strip().upper() for s in RESEARCH_SYMBOL_SPEC.split(",") if s.strip()}
SCOPE_LABEL = "ALL_USDT" if CAPTURE_ALL_USDT else ",".join(sorted(SYMBOLS))

DATA_DIR = pathlib.Path(os.getenv("LIQ_DATA_DIR", "data/forward-liquidations"))
DB_PATH = DATA_DIR / "liquidations.duckdb"
HEALTH_PATH = DATA_DIR / "health.json"
PARQUET_DIR = DATA_DIR / "parquet"
FLUSH_SECONDS = int(os.getenv("LIQ_FLUSH_SECONDS", "60"))
HEALTH_SECONDS = int(os.getenv("LIQ_HEALTH_SECONDS", "30"))
LOG_SECONDS = int(os.getenv("LIQ_LOG_SECONDS", "60"))
STALE_SECONDS = int(os.getenv("LIQ_STALE_SECONDS", "180"))
RECV_POLL_SECONDS = int(os.getenv("LIQ_RECV_POLL_SECONDS", "15"))
SUBSCRIPTION_CHECK_SECONDS = int(os.getenv("LIQ_SUBSCRIPTION_CHECK_SECONDS", "60"))
SUBSCRIPTION_CHECK_ID = 91001


@dataclass
class Health:
    authorization: str = AUTHORIZATION
    liveTrading: bool = False
    connected: bool = False
    subscriptionConfirmed: bool = False
    scope: str = SCOPE_LABEL
    startedAt: str = ""
    lastConnectAt: str | None = None
    lastDisconnectAt: str | None = None
    lastMessageAt: str | None = None
    lastSubscriptionCheckAt: str | None = None
    lastRelevantEventAt: str | None = None
    messagesSeen: int = 0
    controlMessages: int = 0
    liquidationMessages: int = 0
    relevantEvents: int = 0
    filteredEvents: int = 0
    reconnects: int = 0
    parseErrors: int = 0
    storageErrors: int = 0
    lastError: str | None = None
    stale: bool = False


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def utc_day_ms(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).date().isoformat()


def unwrap_payload(payload: dict) -> dict:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def symbol_allowed(symbol: str) -> bool:
    if CAPTURE_ALL_USDT:
        return symbol.endswith("USDT")
    return symbol in SYMBOLS


def normalize_message(payload: dict, received_ms: int) -> dict | None:
    payload = unwrap_payload(payload)
    order = payload.get("o") or {}
    symbol = str(order.get("s") or "").upper()
    if not symbol_allowed(symbol):
        return None
    event_ms = int(payload.get("E") or order.get("T") or received_ms)
    trade_ms = int(order.get("T") or event_ms)
    side = str(order.get("S") or "").upper()
    price = float(order.get("ap") or order.get("p") or 0.0)
    qty = float(order.get("z") or order.get("q") or 0.0)
    if price <= 0 or qty <= 0 or side not in {"BUY", "SELL"}:
        return None
    return {
        "event_ms": event_ms,
        "trade_ms": trade_ms,
        "received_ms": received_ms,
        "symbol": symbol,
        "order_side": side,
        "liquidation_side": "SHORT" if side == "BUY" else "LONG",
        "price": price,
        "quantity": qty,
        "notional": price * qty,
        "status": str(order.get("X") or ""),
        "raw_order_type": str(order.get("o") or ""),
        "time_in_force": str(order.get("f") or ""),
        "day": utc_day_ms(trade_ms),
    }


class Store:
    def __init__(self, path: pathlib.Path):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PARQUET_DIR.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path))
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS liquidations (
              event_ms BIGINT, trade_ms BIGINT, received_ms BIGINT,
              symbol VARCHAR, order_side VARCHAR, liquidation_side VARCHAR,
              price DOUBLE, quantity DOUBLE, notional DOUBLE, status VARCHAR,
              raw_order_type VARCHAR, time_in_force VARCHAR, day VARCHAR,
              ingested_at TIMESTAMP DEFAULT current_timestamp
            )
        """)
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_liq_symbol_trade ON liquidations(symbol, trade_ms)")

    def insert(self, row: dict) -> None:
        keys = ("event_ms","trade_ms","received_ms","symbol","order_side","liquidation_side","price","quantity","notional","status","raw_order_type","time_in_force","day")
        self.con.execute(
            "INSERT INTO liquidations(event_ms,trade_ms,received_ms,symbol,order_side,liquidation_side,price,quantity,notional,status,raw_order_type,time_in_force,day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [row[k] for k in keys],
        )

    def checkpoint(self) -> None:
        self.con.execute("CHECKPOINT")

    def export_days(self) -> None:
        for (day,) in self.con.execute("SELECT DISTINCT day FROM liquidations ORDER BY day").fetchall():
            out = PARQUET_DIR / f"day={day}"
            out.mkdir(parents=True, exist_ok=True)
            target = out / "liquidations.parquet"
            tmp = out / "liquidations.tmp.parquet"
            safe_day = day.replace("'", "''")
            safe_tmp = str(tmp).replace("'", "''")
            self.con.execute(
                f"COPY (SELECT * EXCLUDE(day, ingested_at) FROM liquidations WHERE day='{safe_day}' ORDER BY trade_ms, symbol) TO '{safe_tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            tmp.replace(target)

    def stats(self) -> dict:
        total, latest_ms, total_notional = self.con.execute(
            "SELECT count(*), max(trade_ms), coalesce(sum(notional),0) FROM liquidations"
        ).fetchone()
        by_symbol = {
            row[0]: int(row[1])
            for row in self.con.execute("SELECT symbol, count(*) FROM liquidations GROUP BY symbol ORDER BY count(*) DESC, symbol").fetchall()
        }
        return {
            "rows": int(total or 0),
            "latestTradeMs": int(latest_ms) if latest_ms is not None else None,
            "notional": float(total_notional or 0.0),
            "bySymbol": by_symbol,
            "dbBytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
            "parquetFiles": len(list(PARQUET_DIR.glob("day=*/liquidations.parquet"))),
        }


def write_health(h: Health) -> None:
    HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    if h.lastMessageAt:
        try:
            last_ms = int(dt.datetime.fromisoformat(h.lastMessageAt).timestamp() * 1000)
            h.stale = h.connected and (int(time.time() * 1000) - last_ms > STALE_SECONDS * 1000)
        except Exception:
            h.stale = False
    else:
        h.stale = h.connected
    tmp = HEALTH_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(h), indent=2), encoding="utf-8")
    tmp.replace(HEALTH_PATH)


def log_runtime(h: Health, store: Store) -> None:
    try:
        print(json.dumps({
            "kind": "collector_runtime",
            **asdict(h),
            **store.stats(),
        }, separators=(",", ":")), flush=True)
    except Exception as exc:
        print(json.dumps({"kind":"collector_runtime_error","error":f"{type(exc).__name__}: {exc}"}), flush=True)


async def subscription_check(ws, h: Health) -> None:
    await ws.send(json.dumps({"method":"LIST_SUBSCRIPTIONS","id":SUBSCRIPTION_CHECK_ID}, separators=(",", ":")))
    h.lastSubscriptionCheckAt = iso_now()


async def run_collector(stop: asyncio.Event) -> None:
    store = Store(DB_PATH)
    h = Health(startedAt=iso_now())
    last_flush = time.monotonic()
    last_health = 0.0
    last_log = 0.0
    backoff = 1.0
    print(json.dumps({
        "kind":"collector_start",
        "authorization":AUTHORIZATION,
        "liveTrading":False,
        "scope":SCOPE_LABEL,
        "stream":STREAM_NAME,
        "wsUrl":WS_URL,
        "dataDir":str(DATA_DIR),
        "dbPath":str(DB_PATH),
    }, separators=(",", ":")), flush=True)

    while not stop.is_set():
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20, close_timeout=10, max_queue=4096) as ws:
                h.connected = True
                h.subscriptionConfirmed = False
                h.lastConnectAt = iso_now()
                h.lastError = None
                write_health(h)
                backoff = 1.0
                print(json.dumps({"kind":"collector_connected","at":h.lastConnectAt,"scope":SCOPE_LABEL,"wsUrl":WS_URL}, separators=(",", ":")), flush=True)
                await subscription_check(ws, h)
                last_subscription_check = time.monotonic()

                while not stop.is_set():
                    raw = None
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_POLL_SECONDS)
                    except asyncio.TimeoutError:
                        pass

                    if raw is not None:
                        received_ms = int(time.time() * 1000)
                        h.messagesSeen += 1
                        h.lastMessageAt = iso_now()
                        try:
                            payload = json.loads(raw)
                            if payload.get("id") == SUBSCRIPTION_CHECK_ID:
                                h.controlMessages += 1
                                result = payload.get("result")
                                h.subscriptionConfirmed = isinstance(result, list) and STREAM_NAME in result
                                if not h.subscriptionConfirmed:
                                    h.lastError = f"subscription-check: unexpected result {result!r}"
                                print(json.dumps({"kind":"collector_subscription_check","confirmed":h.subscriptionConfirmed,"result":result}, separators=(",", ":")), flush=True)
                            else:
                                data = unwrap_payload(payload)
                                if data.get("e") == "forceOrder":
                                    h.liquidationMessages += 1
                                row = normalize_message(payload, received_ms)
                                if row:
                                    store.insert(row)
                                    h.relevantEvents += 1
                                    h.lastRelevantEventAt = iso_now()
                                elif data.get("e") == "forceOrder":
                                    h.filteredEvents += 1
                        except Exception as exc:
                            h.parseErrors += 1
                            h.lastError = f"parse: {type(exc).__name__}: {exc}"

                    now = time.monotonic()
                    if now - last_subscription_check >= SUBSCRIPTION_CHECK_SECONDS:
                        await subscription_check(ws, h)
                        last_subscription_check = now
                    if now - last_flush >= FLUSH_SECONDS:
                        try:
                            store.checkpoint()
                            store.export_days()
                        except Exception as exc:
                            h.storageErrors += 1
                            h.lastError = f"storage: {type(exc).__name__}: {exc}"
                        last_flush = now
                    if now - last_health >= HEALTH_SECONDS:
                        write_health(h)
                        last_health = now
                    if now - last_log >= LOG_SECONDS:
                        log_runtime(h, store)
                        last_log = now

        except (asyncio.CancelledError, KeyboardInterrupt):
            break
        except Exception as exc:
            h.connected = False
            h.subscriptionConfirmed = False
            h.lastDisconnectAt = iso_now()
            h.reconnects += 1
            h.lastError = f"socket: {type(exc).__name__}: {exc}"
            write_health(h)
            print(json.dumps({"kind":"collector_disconnected","at":h.lastDisconnectAt,"reconnects":h.reconnects,"error":h.lastError}, separators=(",", ":")), flush=True)
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60.0)

    h.connected = False
    h.subscriptionConfirmed = False
    h.lastDisconnectAt = iso_now()
    try:
        store.checkpoint()
        store.export_days()
    except Exception as exc:
        h.storageErrors += 1
        h.lastError = f"shutdown-storage: {type(exc).__name__}: {exc}"
    write_health(h)
    log_runtime(h, store)


def self_test() -> None:
    sample = {
        "e":"forceOrder","E":1704067200123,
        "o":{"s":"BTCUSDT","S":"SELL","o":"LIMIT","f":"IOC","q":"2.5","p":"42000","ap":"41990","X":"FILLED","T":1704067200000,"z":"2.0"},
    }
    row = normalize_message(sample, 1704067200200)
    assert row and row["liquidation_side"] == "LONG" and abs(row["notional"] - 83980.0) < 1e-9
    assert normalize_message({"stream":STREAM_NAME,"data":sample}, 1704067200200) == row
    assert WS_URL.startswith("wss://fstream.binance.com/market/") or "BINANCE_FORCE_ORDER_WS" in os.environ
    print(json.dumps({"authorization":AUTHORIZATION,"liveTrading":False,"selfTest":"PASS","scope":SCOPE_LABEL,"wsUrl":WS_URL,"row":row}, indent=2))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        self_test()
        return
    stop = asyncio.Event()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    loop.run_until_complete(run_collector(stop))


if __name__ == "__main__":
    main()
