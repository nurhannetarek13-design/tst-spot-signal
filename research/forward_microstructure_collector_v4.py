#!/usr/bin/env python3
"""Research-only USD-M microstructure collector using Binance's 2026 WS routing.

Binance retired legacy USD-M WebSocket routes in April 2026 and split streams:
- /market/ws: ticker, markPrice, aggTrade and other regular market data
- /public/ws: high-frequency public data such as depth

This v4 coordinator keeps v2's storage/feature logic but uses two sockets so
routing matches the current Binance architecture. Open interest remains sourced
from Coinalyze's public research API. Forward labels are built in-process from
later futures-mid snapshots, avoiding REST dependency and DuckDB multi-writer
conflicts. No account/order endpoints are used.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import time

import websockets

import forward_microstructure_collector_v2 as core

AUTHORIZATION = core.AUTHORIZATION
MARKET_WS_URL = os.getenv("MICRO_MARKET_WS_URL", "wss://fstream.binance.com/market/ws")
PUBLIC_WS_URL = os.getenv("MICRO_PUBLIC_WS_URL", "wss://fstream.binance.com/public/ws")
WARMUP_SECONDS = float(os.getenv("MICRO_WARMUP_SECONDS", "5"))
MARKET_BASE_STREAMS = ["!ticker@arr", "!markPrice@arr@1s"]
LABEL_HORIZONS_MIN = (15, 60, 240)
LABEL_TOLERANCE_MS = int(os.getenv("MICRO_LABEL_TOLERANCE_MS", "120000"))


def log(kind: str, **payload) -> None:
    print(json.dumps({"kind": kind, "authorization": AUTHORIZATION, "liveTrading": False, **payload}, separators=(",", ":")), flush=True)


def ensure_label_table(store: core.Store) -> None:
    store.con.execute("""
        CREATE TABLE IF NOT EXISTS microstructure_forward_labels (
          tsMs BIGINT,
          symbol VARCHAR,
          horizonMin INTEGER,
          futureTsMs BIGINT,
          entryMid DOUBLE,
          futureMid DOUBLE,
          grossReturn DOUBLE,
          realizedDelayMs BIGINT,
          labeledAt TIMESTAMP DEFAULT current_timestamp,
          PRIMARY KEY(tsMs, symbol, horizonMin)
        )
    """)
    store.con.execute("CREATE INDEX IF NOT EXISTS idx_micro_labels_symbol_ts ON microstructure_forward_labels(symbol, tsMs)")


def update_forward_labels(store: core.Store) -> dict:
    """Label mature snapshots from later stored futures-mid observations.

    The future observation must be at or just after the requested horizon and
    within LABEL_TOLERANCE_MS. This measures conditional forward return only;
    it is not a trade simulation and does not estimate intrahorizon MFE/MAE.
    """
    before = int(store.con.execute("SELECT count(*) FROM microstructure_forward_labels").fetchone()[0] or 0)
    for horizon in LABEL_HORIZONS_MIN:
        target_ms = horizon * 60_000
        store.con.execute(
            f"""
            INSERT OR IGNORE INTO microstructure_forward_labels
              (tsMs, symbol, horizonMin, futureTsMs, entryMid, futureMid, grossReturn, realizedDelayMs)
            SELECT tsMs, symbol, {horizon}, futureTsMs, entryMid, futureMid,
                   CASE WHEN entryMid > 0 THEN futureMid / entryMid - 1.0 ELSE NULL END,
                   futureTsMs - (tsMs + {target_ms})
            FROM (
              SELECT a.tsMs AS tsMs,
                     a.symbol AS symbol,
                     a.mid AS entryMid,
                     b.tsMs AS futureTsMs,
                     b.mid AS futureMid,
                     row_number() OVER (
                       PARTITION BY a.tsMs, a.symbol
                       ORDER BY b.tsMs ASC
                     ) AS rn
              FROM microstructure a
              JOIN microstructure b
                ON b.symbol = a.symbol
               AND b.tsMs >= a.tsMs + {target_ms}
               AND b.tsMs <= a.tsMs + {target_ms + LABEL_TOLERANCE_MS}
              LEFT JOIN microstructure_forward_labels l
                ON l.tsMs = a.tsMs
               AND l.symbol = a.symbol
               AND l.horizonMin = {horizon}
              WHERE l.tsMs IS NULL
                AND a.mid IS NOT NULL AND a.mid > 0
                AND b.mid IS NOT NULL AND b.mid > 0
            ) q
            WHERE rn = 1
            """
        )
    after = int(store.con.execute("SELECT count(*) FROM microstructure_forward_labels").fetchone()[0] or 0)
    by_horizon = {
        str(int(h)): int(n)
        for h, n in store.con.execute(
            "SELECT horizonMin, count(*) FROM microstructure_forward_labels GROUP BY horizonMin ORDER BY horizonMin"
        ).fetchall()
    }
    return {"labels": after, "newLabels": after - before, "labelsByHorizon": by_horizon}


def export_labels(store: core.Store) -> None:
    out = core.DATA_DIR / "forward-labels.parquet"
    tmp = core.DATA_DIR / "forward-labels.tmp.parquet"
    safe_tmp = str(tmp).replace("'", "''")
    store.con.execute(
        f"COPY (SELECT * FROM microstructure_forward_labels ORDER BY tsMs, symbol, horizonMin) TO '{safe_tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    tmp.replace(out)


async def send_sub(ws, streams: list[str], action: str, req_id: int) -> None:
    if not streams:
        return
    await ws.send(json.dumps({"method": action, "params": streams, "id": req_id}, separators=(",", ":")))


async def sync_symbol_streams(market_ws, public_ws, old: list[str], new: list[str]) -> None:
    old_set, new_set = set(old), set(new)
    remove = sorted(old_set - new_set)
    add = sorted(new_set - old_set)
    if remove:
        await send_sub(market_ws, [f"{s.lower()}@aggTrade" for s in remove], "UNSUBSCRIBE", 94001)
        await send_sub(public_ws, [f"{s.lower()}@depth{core.DEPTH_LEVELS}@500ms" for s in remove], "UNSUBSCRIBE", 94002)
    if add:
        await send_sub(market_ws, [f"{s.lower()}@aggTrade" for s in add], "SUBSCRIBE", 94003)
        await send_sub(public_ws, [f"{s.lower()}@depth{core.DEPTH_LEVELS}@500ms" for s in add], "SUBSCRIBE", 94004)


async def receiver(name: str, ws, state: core.State) -> None:
    while True:
        raw = await ws.recv()
        payload = json.loads(raw)
        if isinstance(payload, dict) and ("result" in payload or "code" in payload):
            log("microstructure_ws_control", socket=name, payload=payload)
            continue
        try:
            core.handle_payload(state, payload)
        except Exception as exc:
            state.parse_errors += 1
            state.last_error = f"{name}-message: {type(exc).__name__}: {exc}"


async def run(stop: asyncio.Event) -> None:
    store = core.Store()
    ensure_label_table(store)
    eligible, eligible_meta = await asyncio.to_thread(core.discover_eligible_markets)
    state = core.State(eligible, eligible_meta)
    last_export_day = None
    universe_changed_at = 0.0

    log(
        "microstructure_collector_start",
        version=4,
        transport="Binance 2026 dual WebSocket routing + Coinalyze OI",
        marketWsUrl=MARKET_WS_URL,
        publicWsUrl=PUBLIC_WS_URL,
        topN=core.TOP_N,
        minQuoteVolume24h=core.MIN_QUOTE_VOLUME,
        pollSeconds=core.POLL_SECONDS,
        warmupSeconds=WARMUP_SECONDS,
        labelHorizonsMin=list(LABEL_HORIZONS_MIN),
        eligibleMeta=eligible_meta,
    )

    backoff = 1.0
    while not stop.is_set():
        market_task = None
        public_task = None
        try:
            async with websockets.connect(
                MARKET_WS_URL, ping_interval=20, ping_timeout=20, close_timeout=10,
                max_queue=8192, max_size=None,
            ) as market_ws, websockets.connect(
                PUBLIC_WS_URL, ping_interval=20, ping_timeout=20, close_timeout=10,
                max_queue=8192, max_size=None,
            ) as public_ws:
                await send_sub(market_ws, MARKET_BASE_STREAMS, "SUBSCRIBE", 94000)
                log("microstructure_ws_connected", market=True, public=True)
                backoff = 1.0
                state.last_error = None
                market_task = asyncio.create_task(receiver("market", market_ws, state))
                public_task = asyncio.create_task(receiver("public", public_ws, state))

                while not stop.is_set():
                    await asyncio.sleep(0.5)
                    if market_task.done():
                        exc = market_task.exception()
                        raise RuntimeError(f"market receiver stopped: {exc}")
                    if public_task.done():
                        exc = public_task.exception()
                        raise RuntimeError(f"public receiver stopped: {exc}")

                    mono = time.monotonic()
                    need_universe = (
                        (not state.universe and len(state.ticker_qv) >= 10)
                        or (state.universe and mono - state.last_universe_refresh >= core.UNIVERSE_REFRESH_SECONDS)
                    )
                    if need_universe:
                        new = state.select_universe()
                        if new:
                            old = list(state.universe)
                            if new != old:
                                await sync_symbol_streams(market_ws, public_ws, old, new)
                                state.universe = new
                                universe_changed_at = mono
                                log(
                                    "microstructure_universe",
                                    symbols=new,
                                    quoteVolumes={s: state.ticker_qv.get(s) for s in new},
                                    eligibleMeta=eligible_meta,
                                )
                            state.last_universe_refresh = mono

                    if not state.universe:
                        continue
                    warmed = universe_changed_at > 0 and mono - universe_changed_at >= WARMUP_SECONDS
                    due = state.last_snapshot == 0.0 or mono - state.last_snapshot >= core.POLL_SECONDS
                    if not warmed or not due:
                        continue

                    ts = core.now_ms()
                    oi_usd, oi_error = await asyncio.to_thread(core.fetch_open_interest_usd, state.universe, state.eligible)
                    if oi_error:
                        state.oi_errors += 1
                        state.last_error = f"oi: {oi_error}"
                    rows, row_errors = core.build_rows(state, oi_usd, ts)
                    label_stats = {"labels": 0, "newLabels": 0, "labelsByHorizon": {}}
                    try:
                        store.insert_many(rows)
                        label_stats = update_forward_labels(store)
                        store.checkpoint()
                        today = core.dt.datetime.fromtimestamp(ts / 1000, tz=core.dt.timezone.utc).date().isoformat()
                        if rows and (last_export_day != today or state.cycles % 10 == 0):
                            store.export_day(today)
                            export_labels(store)
                            last_export_day = today
                    except Exception as exc:
                        state.storage_errors += 1
                        state.last_error = f"storage: {type(exc).__name__}: {exc}"
                    state.cycles += 1
                    state.last_snapshot = mono
                    state.last_snapshot_at = core.iso_now()
                    stats = store.stats()
                    health = {
                        "authorization": AUTHORIZATION,
                        "liveTrading": False,
                        "healthy": bool(rows),
                        "version": 4,
                        "transport": "dual-websocket-2026-routing",
                        "generatedAt": core.iso_now(),
                        "lastSnapshotAt": state.last_snapshot_at,
                        "cycles": state.cycles,
                        "wsMessages": state.ws_messages,
                        "parseErrors": state.parse_errors,
                        "storageErrors": state.storage_errors,
                        "oiErrors": state.oi_errors,
                        "reconnects": state.reconnects,
                        "lastError": state.last_error,
                        "universe": state.universe,
                        "rowsThisCycle": len(rows),
                        "rowErrorsThisCycle": len(row_errors),
                        "rowErrorsSample": row_errors[:10],
                        "oiCoverage": len(oi_usd),
                        "eligibleMeta": eligible_meta,
                        **label_stats,
                        **stats,
                    }
                    core.write_health(health)
                    print(json.dumps({"kind": "microstructure_runtime", **health}, separators=(",", ":")), flush=True)

        except (asyncio.CancelledError, KeyboardInterrupt):
            break
        except Exception as exc:
            state.reconnects += 1
            state.last_error = f"socket: {type(exc).__name__}: {exc}"
            log("microstructure_ws_disconnected", reconnects=state.reconnects, error=state.last_error)
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60.0)
        finally:
            for task in (market_task, public_task):
                if task is not None and not task.done():
                    task.cancel()


def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    loop.run_until_complete(run(stop))


if __name__ == "__main__":
    main()
