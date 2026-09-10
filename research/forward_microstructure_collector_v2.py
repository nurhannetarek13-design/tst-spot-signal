#!/usr/bin/env python3
"""Research-only Binance USD-M microstructure collector (WebSocket-first).

Why v2 exists: Binance USD-M REST is geo-blocked from the current Railway
region while the public fstream WebSocket is reachable. This collector avoids
USD-M REST entirely. It collects:
- 24h quote-volume from !ticker@arr for dynamic liquidity ranking
- mark/index/funding from !markPrice@arr@1s
- partial 20-level depth for selected symbols
- aggregate trades to reconstruct 5-minute taker buy/sell flow
- current USD open interest from Coinalyze (existing research API key)

No order endpoints, account endpoints, private keys, signals, or live trading.
"""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import json
import math
import os
import pathlib
import random
import signal
import time
import urllib.error
import urllib.parse
import urllib.request

import duckdb
import websockets

AUTHORIZATION = "RESEARCH_ONLY"
WS_URL = os.getenv("MICRO_WS_URL", "wss://fstream.binance.com/ws")
TOP_N = int(os.getenv("MICRO_TOP_N", "20"))
MIN_QUOTE_VOLUME = float(os.getenv("MICRO_MIN_QUOTE_VOLUME", "20000000"))
POLL_SECONDS = int(os.getenv("MICRO_POLL_SECONDS", "60"))
UNIVERSE_REFRESH_SECONDS = int(os.getenv("MICRO_UNIVERSE_REFRESH_SECONDS", "3600"))
DEPTH_LEVELS = int(os.getenv("MICRO_DEPTH_LIMIT", "20"))
PROXY_PORT = int(os.getenv("PROXY_PORT", "8080"))
PROXY_BASE = os.getenv("BINANCE_PUBLIC_PROXY_URL", f"http://127.0.0.1:{PROXY_PORT}/api/binance-public")
COINALYZE_BASE = "https://api.coinalyze.net/v1"
COINALYZE_KEY = os.getenv("COINALYZE_API_KEY", "").strip()
DATA_DIR = pathlib.Path(os.getenv("MICRO_DATA_DIR", "/data/microstructure"))
DB_PATH = DATA_DIR / "microstructure.duckdb"
HEALTH_PATH = DATA_DIR / "health.json"
PARQUET_DIR = DATA_DIR / "parquet"
FIVE_MIN_MS = 5 * 60 * 1000
STALE_DEPTH_MS = 10_000
STALE_MARK_MS = 10_000
SUBSCRIBE_ID = 92001
UNSUBSCRIBE_ID = 92002
BASE_STREAMS = ["!ticker@arr", "!markPrice@arr@1s"]


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def http_json(url: str, headers: dict | None = None, attempts: int = 6, timeout: int = 20):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers or {"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 429:
                retry = int(exc.headers.get("Retry-After", "2") or "2")
                time.sleep(max(1, min(retry, 60)))
            else:
                time.sleep(min(8.0, 0.5 * (2 ** i)))
        except Exception as exc:
            last = exc
            time.sleep(min(8.0, 0.5 * (2 ** i)))
    raise RuntimeError(f"HTTP failed: {url}: {last}")


def proxy_get(path: str):
    url = PROXY_BASE + "?" + urllib.parse.urlencode({"path": path})
    return http_json(url, {"Accept": "application/json", "User-Agent": "tst-microstructure-v2"}, attempts=8)


def coinalyze_get(path: str, params: dict | None = None):
    if not COINALYZE_KEY:
        raise RuntimeError("COINALYZE_API_KEY is missing")
    q = urllib.parse.urlencode(params or {})
    url = f"{COINALYZE_BASE}/{path}" + (f"?{q}" if q else "")
    return http_json(
        url,
        {"api_key": COINALYZE_KEY, "Accept": "application/json", "User-Agent": "tst-microstructure-v2"},
        attempts=6,
    )


def discover_eligible_markets() -> tuple[dict[str, dict], dict]:
    """Intersect Binance stable perpetuals on Coinalyze with Binance Spot crypto bases.

    The spot intersection intentionally excludes new TradFi-style USD-M contracts
    while still retaining multiplier contracts such as 1000PEPE when Coinalyze's
    base_asset maps back to a Binance spot crypto base.
    """
    spot = proxy_get("/api/v3/exchangeInfo")
    spot_bases = {
        str(s.get("baseAsset") or "").upper()
        for s in (spot.get("symbols") or [])
        if str(s.get("quoteAsset") or "").upper() == "USDT"
        and str(s.get("status") or "").upper() == "TRADING"
    }
    exchanges = coinalyze_get("exchanges")
    matches = [x for x in exchanges if "binance" in str(x.get("name") or "").lower()]
    exact = [x for x in matches if str(x.get("name") or "").strip().lower() == "binance"]
    chosen = exact[0] if exact else (matches[0] if matches else None)
    if not chosen or not chosen.get("code"):
        raise RuntimeError("Binance exchange code not found in Coinalyze")
    code = str(chosen["code"])
    markets = coinalyze_get("future-markets")
    eligible: dict[str, dict] = {}
    for m in markets:
        if str(m.get("exchange") or "") != code:
            continue
        if not bool(m.get("is_perpetual")) or str(m.get("margined") or "").upper() != "STABLE":
            continue
        symbol = str(m.get("symbol_on_exchange") or "").upper()
        base = str(m.get("base_asset") or "").upper()
        quote = str(m.get("quote_asset") or "").upper()
        if not symbol.endswith("USDT") or quote not in {"USDT", "USD", ""}:
            continue
        if base not in spot_bases:
            continue
        cz_symbol = str(m.get("symbol") or "")
        if not cz_symbol:
            continue
        eligible[symbol] = {"symbol": symbol, "baseAsset": base, "coinalyzeSymbol": cz_symbol}
    meta = {
        "binanceCode": code,
        "spotCryptoBases": len(spot_bases),
        "eligiblePerpetuals": len(eligible),
        "rule": "Coinalyze Binance stable perpetual AND base asset has active Binance Spot USDT market",
    }
    return eligible, meta


def fetch_open_interest_usd(universe: list[str], eligible: dict[str, dict]) -> tuple[dict[str, float], str | None]:
    if not universe:
        return {}, None
    mapped = [eligible[s]["coinalyzeSymbol"] for s in universe if s in eligible]
    if not mapped:
        return {}, "no mapped Coinalyze symbols"
    reverse = {eligible[s]["coinalyzeSymbol"]: s for s in universe if s in eligible}
    try:
        payload = coinalyze_get("open-interest", {"symbols": ",".join(mapped[:20]), "convert_to_usd": "true"})
        out: dict[str, float] = {}
        for item in payload if isinstance(payload, list) else []:
            local = reverse.get(str(item.get("symbol") or ""))
            if local:
                value = float(item.get("value") or 0.0)
                if math.isfinite(value) and value >= 0:
                    out[local] = value
        return out, None
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def depth_features(payload: dict) -> dict:
    bids = [(float(x[0]), float(x[1])) for x in (payload.get("b") or []) if len(x) >= 2]
    asks = [(float(x[0]), float(x[1])) for x in (payload.get("a") or []) if len(x) >= 2]
    if not bids or not asks:
        raise ValueError("empty depth")
    bid, bid_qty = bids[0]
    ask, ask_qty = asks[0]
    mid = (bid + ask) / 2.0
    if mid <= 0 or ask < bid:
        raise ValueError("invalid top of book")
    denom_top = bid_qty + ask_qty
    micro = (ask * bid_qty + bid * ask_qty) / denom_top if denom_top > 0 else mid
    out = {
        "bestBid": bid,
        "bestAsk": ask,
        "bestBidQty": bid_qty,
        "bestAskQty": ask_qty,
        "mid": mid,
        "spreadBps": (ask - bid) / mid * 10000.0,
        "microprice": micro,
        "micropriceMidBps": (micro - mid) / mid * 10000.0,
    }
    for n in (5, 10, 20):
        bn = sum(p * q for p, q in bids[:n])
        an = sum(p * q for p, q in asks[:n])
        d = bn + an
        out[f"bidNotional{n}"] = bn
        out[f"askNotional{n}"] = an
        out[f"imbalance{n}"] = (bn - an) / d if d > 0 else None
    return out


COLUMNS = [
    "tsMs", "symbol", "baseAsset", "quoteVolume24h",
    "bestBid", "bestAsk", "bestBidQty", "bestAskQty", "mid", "spreadBps", "microprice", "micropriceMidBps",
    "bidNotional5", "askNotional5", "imbalance5", "bidNotional10", "askNotional10", "imbalance10",
    "bidNotional20", "askNotional20", "imbalance20", "openInterest", "openInterestNotional", "oiSource",
    "markPrice", "indexPrice", "basisBps", "lastFundingRate", "nextFundingTime",
    "takerBuyVol5m", "takerSellVol5m", "takerBuySellRatio5m", "takerBuyShare5m", "takerTimestamp", "day",
]


class Store:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PARQUET_DIR.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(DB_PATH))
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS microstructure (
              tsMs BIGINT, symbol VARCHAR, baseAsset VARCHAR, quoteVolume24h DOUBLE,
              bestBid DOUBLE, bestAsk DOUBLE, bestBidQty DOUBLE, bestAskQty DOUBLE, mid DOUBLE, spreadBps DOUBLE,
              microprice DOUBLE, micropriceMidBps DOUBLE,
              bidNotional5 DOUBLE, askNotional5 DOUBLE, imbalance5 DOUBLE,
              bidNotional10 DOUBLE, askNotional10 DOUBLE, imbalance10 DOUBLE,
              bidNotional20 DOUBLE, askNotional20 DOUBLE, imbalance20 DOUBLE,
              openInterest DOUBLE, openInterestNotional DOUBLE, markPrice DOUBLE, indexPrice DOUBLE, basisBps DOUBLE,
              lastFundingRate DOUBLE, nextFundingTime BIGINT,
              takerBuyVol5m DOUBLE, takerSellVol5m DOUBLE, takerBuySellRatio5m DOUBLE, takerBuyShare5m DOUBLE,
              takerTimestamp BIGINT, day VARCHAR, ingestedAt TIMESTAMP DEFAULT current_timestamp
            )
        """)
        self.con.execute("ALTER TABLE microstructure ADD COLUMN IF NOT EXISTS oiSource VARCHAR")
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_micro_symbol_ts ON microstructure(symbol, tsMs)")

    def insert_many(self, rows: list[dict]) -> None:
        if not rows:
            return
        sql = f"INSERT INTO microstructure({','.join(COLUMNS)}) VALUES ({','.join('?' for _ in COLUMNS)})"
        self.con.executemany(sql, [[r.get(k) for k in COLUMNS] for r in rows])

    def checkpoint(self) -> None:
        self.con.execute("CHECKPOINT")

    def export_day(self, day: str) -> None:
        out = PARQUET_DIR / f"day={day}"
        out.mkdir(parents=True, exist_ok=True)
        target = out / "microstructure.parquet"
        tmp = out / "microstructure.tmp.parquet"
        safe_day = day.replace("'", "''")
        safe_tmp = str(tmp).replace("'", "''")
        self.con.execute(
            f"COPY (SELECT * EXCLUDE(day, ingestedAt) FROM microstructure WHERE day='{safe_day}' ORDER BY tsMs, symbol) "
            f"TO '{safe_tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        tmp.replace(target)

    def stats(self) -> dict:
        n, symbols, latest = self.con.execute("SELECT count(*), count(DISTINCT symbol), max(tsMs) FROM microstructure").fetchone()
        return {"rows": int(n or 0), "symbolsStored": int(symbols or 0), "latestTsMs": int(latest) if latest is not None else None}


def write_health(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HEALTH_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(HEALTH_PATH)


class State:
    def __init__(self, eligible: dict[str, dict], eligible_meta: dict):
        self.eligible = eligible
        self.eligible_meta = eligible_meta
        self.ticker_qv: dict[str, float] = {}
        self.mark: dict[str, dict] = {}
        self.depth: dict[str, dict] = {}
        self.trades: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.universe: list[str] = []
        self.last_universe_refresh = 0.0
        self.last_snapshot = 0.0
        self.cycles = 0
        self.ws_messages = 0
        self.parse_errors = 0
        self.storage_errors = 0
        self.oi_errors = 0
        self.reconnects = 0
        self.last_error: str | None = None
        self.last_snapshot_at: str | None = None

    def select_universe(self) -> list[str]:
        ranked = [
            (qv, symbol)
            for symbol, qv in self.ticker_qv.items()
            if symbol in self.eligible and math.isfinite(qv) and qv >= MIN_QUOTE_VOLUME
        ]
        ranked.sort(key=lambda x: (-x[0], x[1]))
        return [s for _, s in ranked[:TOP_N]]

    def prune_trades(self, symbol: str, cutoff_ms: int) -> None:
        dq = self.trades[symbol]
        while dq and dq[0][0] < cutoff_ms:
            dq.popleft()

    def flow_5m(self, symbol: str, ts_ms: int) -> tuple[float, float, int | None]:
        self.prune_trades(symbol, ts_ms - FIVE_MIN_MS)
        buy = 0.0
        sell = 0.0
        latest = None
        for trade_ms, side, notional in self.trades[symbol]:
            latest = trade_ms
            if side == "BUY":
                buy += notional
            else:
                sell += notional
        return buy, sell, latest


async def update_symbol_streams(ws, old: list[str], new: list[str]) -> None:
    old_set, new_set = set(old), set(new)
    remove = sorted(old_set - new_set)
    add = sorted(new_set - old_set)
    if remove:
        params = []
        for s in remove:
            low = s.lower()
            params.extend([f"{low}@depth{DEPTH_LEVELS}@500ms", f"{low}@aggTrade"])
        await ws.send(json.dumps({"method": "UNSUBSCRIBE", "params": params, "id": UNSUBSCRIBE_ID}, separators=(",", ":")))
    if add:
        params = []
        for s in add:
            low = s.lower()
            params.extend([f"{low}@depth{DEPTH_LEVELS}@500ms", f"{low}@aggTrade"])
        await ws.send(json.dumps({"method": "SUBSCRIBE", "params": params, "id": SUBSCRIBE_ID}, separators=(",", ":")))


def handle_payload(state: State, payload) -> None:
    state.ws_messages += 1
    if isinstance(payload, list):
        if not payload:
            return
        event = str(payload[0].get("e") or "") if isinstance(payload[0], dict) else ""
        if event == "24hrTicker":
            for x in payload:
                symbol = str(x.get("s") or "").upper()
                if symbol in state.eligible:
                    try:
                        state.ticker_qv[symbol] = float(x.get("q") or 0.0)
                    except Exception:
                        pass
        elif event == "markPriceUpdate":
            recv = now_ms()
            for x in payload:
                symbol = str(x.get("s") or "").upper()
                if symbol in state.eligible:
                    state.mark[symbol] = {**x, "_receivedMs": recv}
        return

    if not isinstance(payload, dict):
        return
    if "result" in payload and "id" in payload:
        return
    event = str(payload.get("e") or "")
    symbol = str(payload.get("s") or "").upper()
    if event == "depthUpdate" and symbol:
        state.depth[symbol] = {**payload, "_receivedMs": now_ms()}
    elif event == "aggTrade" and symbol:
        try:
            trade_ms = int(payload.get("T") or payload.get("E") or now_ms())
            price = float(payload.get("p") or 0.0)
            qty = float(payload.get("q") or 0.0)
            if price <= 0 or qty <= 0:
                return
            # m=true => buyer is maker => aggressive/taker side is SELL.
            side = "SELL" if bool(payload.get("m")) else "BUY"
            state.trades[symbol].append((trade_ms, side, price * qty))
        except Exception:
            state.parse_errors += 1


def build_rows(state: State, oi_usd: dict[str, float], ts_ms: int) -> tuple[list[dict], list[dict]]:
    rows = []
    errors = []
    for symbol in state.universe:
        try:
            depth = state.depth.get(symbol)
            mark = state.mark.get(symbol)
            if not depth or ts_ms - int(depth.get("_receivedMs") or 0) > STALE_DEPTH_MS:
                raise ValueError("depth missing/stale")
            if not mark or ts_ms - int(mark.get("_receivedMs") or 0) > STALE_MARK_MS:
                raise ValueError("mark price missing/stale")
            d = depth_features(depth)
            mark_price = float(mark.get("p") or 0.0)
            index_price = float(mark.get("i") or 0.0)
            funding = float(mark.get("r") or 0.0)
            buy, sell, latest_trade = state.flow_5m(symbol, ts_ms)
            total = buy + sell
            row = {
                "tsMs": ts_ms,
                "symbol": symbol,
                "baseAsset": state.eligible[symbol]["baseAsset"],
                "quoteVolume24h": float(state.ticker_qv.get(symbol) or 0.0),
                **d,
                "openInterest": None,
                "openInterestNotional": oi_usd.get(symbol),
                "oiSource": "Coinalyze" if symbol in oi_usd else None,
                "markPrice": mark_price,
                "indexPrice": index_price,
                "basisBps": (mark_price - index_price) / index_price * 10000.0 if index_price > 0 else None,
                "lastFundingRate": funding,
                "nextFundingTime": int(mark.get("T") or 0),
                "takerBuyVol5m": buy,
                "takerSellVol5m": sell,
                "takerBuySellRatio5m": buy / sell if sell > 0 else None,
                "takerBuyShare5m": buy / total if total > 0 else None,
                "takerTimestamp": latest_trade,
                "day": dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).date().isoformat(),
            }
            rows.append(row)
        except Exception as exc:
            errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
    return rows, errors


async def run(stop: asyncio.Event) -> None:
    store = Store()
    eligible, eligible_meta = await asyncio.to_thread(discover_eligible_markets)
    state = State(eligible, eligible_meta)
    print(json.dumps({
        "kind": "microstructure_collector_start",
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "version": 2,
        "transport": "Binance USD-M public WebSocket + Coinalyze OI",
        "wsUrl": WS_URL,
        "topN": TOP_N,
        "minQuoteVolume24h": MIN_QUOTE_VOLUME,
        "pollSeconds": POLL_SECONDS,
        "eligibleMeta": eligible_meta,
    }, separators=(",", ":")), flush=True)

    backoff = 1.0
    last_export_day = None
    while not stop.is_set():
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20, close_timeout=10, max_queue=8192) as ws:
                await ws.send(json.dumps({"method": "SUBSCRIBE", "params": BASE_STREAMS, "id": 92000}, separators=(",", ":")))
                backoff = 1.0
                state.last_error = None
                print(json.dumps({"kind": "microstructure_ws_connected", "authorization": AUTHORIZATION}, separators=(",", ":")), flush=True)

                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        payload = json.loads(raw)
                        handle_payload(state, payload)
                    except asyncio.TimeoutError:
                        pass
                    except Exception as exc:
                        state.parse_errors += 1
                        state.last_error = f"message: {type(exc).__name__}: {exc}"

                    now_mono = time.monotonic()
                    if (not state.universe and len(state.ticker_qv) >= 10) or now_mono - state.last_universe_refresh >= UNIVERSE_REFRESH_SECONDS:
                        new = state.select_universe()
                        if new:
                            old = list(state.universe)
                            await update_symbol_streams(ws, old, new)
                            state.universe = new
                            state.last_universe_refresh = now_mono
                            print(json.dumps({
                                "kind": "microstructure_universe",
                                "authorization": AUTHORIZATION,
                                "liveTrading": False,
                                "symbols": new,
                                "quoteVolumes": {s: state.ticker_qv.get(s) for s in new},
                                **eligible_meta,
                            }, separators=(",", ":")), flush=True)

                    if state.universe and (state.last_snapshot == 0.0 or now_mono - state.last_snapshot >= POLL_SECONDS):
                        ts = now_ms()
                        oi_usd, oi_error = await asyncio.to_thread(fetch_open_interest_usd, state.universe, state.eligible)
                        if oi_error:
                            state.oi_errors += 1
                            state.last_error = f"oi: {oi_error}"
                        rows, row_errors = build_rows(state, oi_usd, ts)
                        try:
                            store.insert_many(rows)
                            store.checkpoint()
                            today = dt.datetime.fromtimestamp(ts / 1000, tz=dt.timezone.utc).date().isoformat()
                            if rows and (last_export_day != today or state.cycles % 10 == 0):
                                store.export_day(today)
                                last_export_day = today
                        except Exception as exc:
                            state.storage_errors += 1
                            state.last_error = f"storage: {type(exc).__name__}: {exc}"
                        state.cycles += 1
                        state.last_snapshot = now_mono
                        state.last_snapshot_at = iso_now()
                        stats = store.stats()
                        health = {
                            "authorization": AUTHORIZATION,
                            "liveTrading": False,
                            "healthy": bool(rows),
                            "version": 2,
                            "transport": "websocket-first",
                            "generatedAt": iso_now(),
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
                            **stats,
                        }
                        write_health(health)
                        print(json.dumps({"kind": "microstructure_runtime", **health}, separators=(",", ":")), flush=True)
        except (asyncio.CancelledError, KeyboardInterrupt):
            break
        except Exception as exc:
            state.reconnects += 1
            state.last_error = f"socket: {type(exc).__name__}: {exc}"
            print(json.dumps({
                "kind": "microstructure_ws_disconnected",
                "authorization": AUTHORIZATION,
                "reconnects": state.reconnects,
                "error": state.last_error,
            }, separators=(",", ":")), flush=True)
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60.0)


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
