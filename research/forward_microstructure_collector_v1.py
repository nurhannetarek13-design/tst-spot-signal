#!/usr/bin/env python3
"""Research-only USD-M microstructure snapshot collector.

Collects a frozen-at-each-refresh top-liquidity universe of active USDT perpetual
crypto contracts and stores public depth, open-interest, premium/funding and
taker-flow features. No order endpoints, API keys, signals, or live trading.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import signal
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import duckdb

AUTHORIZATION = "RESEARCH_ONLY"
TOP_N = int(os.getenv("MICRO_TOP_N", "20"))
POLL_SECONDS = int(os.getenv("MICRO_POLL_SECONDS", "60"))
UNIVERSE_REFRESH_SECONDS = int(os.getenv("MICRO_UNIVERSE_REFRESH_SECONDS", "3600"))
DEPTH_LIMIT = int(os.getenv("MICRO_DEPTH_LIMIT", "20"))
MAX_WORKERS = int(os.getenv("MICRO_MAX_WORKERS", "6"))
PROXY_PORT = int(os.getenv("PROXY_PORT", "8080"))
PROXY_BASE = os.getenv("BINANCE_PUBLIC_PROXY_URL", f"http://127.0.0.1:{PROXY_PORT}/api/binance-public")
DATA_DIR = pathlib.Path(os.getenv("MICRO_DATA_DIR", "/data/microstructure"))
DB_PATH = DATA_DIR / "microstructure.duckdb"
HEALTH_PATH = DATA_DIR / "health.json"
PARQUET_DIR = DATA_DIR / "parquet"
STABLE_BASES = {
    "USDT", "USDC", "FDUSD", "TUSD", "USDP", "DAI", "USD1", "BFUSD", "USDE", "USDS",
}
stop_event = threading.Event()


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def proxy_get(path: str, attempts: int = 4, timeout: int = 15):
    url = PROXY_BASE + "?" + urllib.parse.urlencode({"path": path})
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "tst-microstructure-v1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as exc:
            last = exc
            time.sleep(min(5.0, 0.5 * (2 ** i)))
    raise RuntimeError(f"GET {path} failed: {last}")


def active_crypto_contracts(exchange_info: dict) -> dict[str, dict]:
    out = {}
    for s in exchange_info.get("symbols") or []:
        symbol = str(s.get("symbol") or "").upper()
        base = str(s.get("baseAsset") or "").upper()
        quote = str(s.get("quoteAsset") or "").upper()
        contract_type = str(s.get("contractType") or "").upper()
        status = str(s.get("status") or "").upper()
        underlying_type = str(s.get("underlyingType") or "").upper()
        if not symbol or quote != "USDT" or contract_type != "PERPETUAL" or status != "TRADING":
            continue
        if base in STABLE_BASES:
            continue
        # New USD-M product families can include non-crypto underlyings. If Binance
        # labels the type, retain only crypto/coin contracts; old records may omit it.
        if underlying_type and underlying_type not in {"COIN", "CRYPTO", ""}:
            continue
        out[symbol] = {
            "symbol": symbol,
            "baseAsset": base,
            "quoteAsset": quote,
            "underlyingType": underlying_type or None,
        }
    return out


def select_universe() -> tuple[list[dict], dict]:
    exchange = proxy_get("/fapi/v1/exchangeInfo")
    eligible = active_crypto_contracts(exchange)
    tickers = proxy_get("/fapi/v1/ticker/24hr")
    ranked = []
    for t in tickers if isinstance(tickers, list) else []:
        symbol = str(t.get("symbol") or "").upper()
        if symbol not in eligible:
            continue
        try:
            qv = float(t.get("quoteVolume") or 0.0)
        except Exception:
            qv = 0.0
        if not math.isfinite(qv) or qv <= 0:
            continue
        ranked.append({**eligible[symbol], "quoteVolume24h": qv})
    ranked.sort(key=lambda x: (-x["quoteVolume24h"], x["symbol"]))
    chosen = ranked[:TOP_N]
    meta = {
        "eligibleContracts": len(eligible),
        "rankedContracts": len(ranked),
        "topN": TOP_N,
        "rule": "TRADING + PERPETUAL + quoteAsset=USDT + crypto/coin underlying + non-stable base; rank by 24h quoteVolume",
    }
    return chosen, meta


def depth_features(depth: dict) -> dict:
    bids = [(float(p), float(q)) for p, q, *_ in (depth.get("bids") or []) if float(p) > 0 and float(q) >= 0]
    asks = [(float(p), float(q)) for p, q, *_ in (depth.get("asks") or []) if float(p) > 0 and float(q) >= 0]
    if not bids or not asks:
        raise ValueError("empty order book")
    bid, bid_qty = bids[0]
    ask, ask_qty = asks[0]
    mid = (bid + ask) / 2.0
    if mid <= 0 or ask < bid:
        raise ValueError("invalid top of book")
    micro = (ask * bid_qty + bid * ask_qty) / (bid_qty + ask_qty) if bid_qty + ask_qty > 0 else mid
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
        denom = bn + an
        out[f"bidNotional{n}"] = bn
        out[f"askNotional{n}"] = an
        out[f"imbalance{n}"] = (bn - an) / denom if denom > 0 else None
    return out


def snapshot_symbol(item: dict, ts_ms: int) -> dict:
    symbol = item["symbol"]
    qs = urllib.parse.quote(symbol, safe="")
    depth = proxy_get(f"/fapi/v1/depth?symbol={qs}&limit={DEPTH_LIMIT}")
    oi = proxy_get(f"/fapi/v1/openInterest?symbol={qs}")
    premium = proxy_get(f"/fapi/v1/premiumIndex?symbol={qs}")
    taker = proxy_get(f"/futures/data/takerlongshortRatio?symbol={qs}&period=5m&limit=1")
    d = depth_features(depth)
    mark = float(premium.get("markPrice") or 0.0)
    index = float(premium.get("indexPrice") or 0.0)
    open_interest = float(oi.get("openInterest") or 0.0)
    funding = float(premium.get("lastFundingRate") or 0.0)
    tk = taker[-1] if isinstance(taker, list) and taker else {}
    buy_vol = float(tk.get("buyVol") or 0.0)
    sell_vol = float(tk.get("sellVol") or 0.0)
    taker_total = buy_vol + sell_vol
    ratio_raw = tk.get("buySellRatio")
    buy_sell_ratio = float(ratio_raw) if ratio_raw not in (None, "") else (buy_vol / sell_vol if sell_vol > 0 else None)
    return {
        "tsMs": ts_ms,
        "day": dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).date().isoformat(),
        "symbol": symbol,
        "baseAsset": item.get("baseAsset"),
        "quoteVolume24h": float(item.get("quoteVolume24h") or 0.0),
        **d,
        "openInterest": open_interest,
        "openInterestNotional": open_interest * mark if mark > 0 else None,
        "markPrice": mark,
        "indexPrice": index,
        "basisBps": (mark - index) / index * 10000.0 if index > 0 else None,
        "lastFundingRate": funding,
        "nextFundingTime": int(premium.get("nextFundingTime") or 0),
        "takerBuyVol5m": buy_vol,
        "takerSellVol5m": sell_vol,
        "takerBuySellRatio5m": buy_sell_ratio,
        "takerBuyShare5m": buy_vol / taker_total if taker_total > 0 else None,
        "takerTimestamp": int(tk.get("timestamp") or 0),
    }


COLUMNS = [
    "tsMs", "symbol", "baseAsset", "quoteVolume24h",
    "bestBid", "bestAsk", "bestBidQty", "bestAskQty", "mid", "spreadBps", "microprice", "micropriceMidBps",
    "bidNotional5", "askNotional5", "imbalance5", "bidNotional10", "askNotional10", "imbalance10",
    "bidNotional20", "askNotional20", "imbalance20", "openInterest", "openInterestNotional", "markPrice", "indexPrice",
    "basisBps", "lastFundingRate", "nextFundingTime", "takerBuyVol5m", "takerSellVol5m", "takerBuySellRatio5m",
    "takerBuyShare5m", "takerTimestamp", "day",
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
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_micro_symbol_ts ON microstructure(symbol, tsMs)")

    def insert_many(self, rows: list[dict]) -> None:
        if not rows:
            return
        ph = ",".join("?" for _ in COLUMNS)
        sql = f"INSERT INTO microstructure({','.join(COLUMNS)}) VALUES ({ph})"
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


def main() -> None:
    store = Store()
    universe: list[dict] = []
    universe_meta = {}
    last_universe = 0.0
    last_export_day = None
    cycles = 0
    total_errors = 0
    consecutive_empty = 0
    print(json.dumps({
        "kind": "microstructure_collector_start", "authorization": AUTHORIZATION, "liveTrading": False,
        "topN": TOP_N, "pollSeconds": POLL_SECONDS, "depthLimit": DEPTH_LIMIT, "proxy": PROXY_BASE,
    }, separators=(",", ":")), flush=True)

    while not stop_event.is_set():
        cycle_start = time.monotonic()
        try:
            if not universe or cycle_start - last_universe >= UNIVERSE_REFRESH_SECONDS:
                universe, universe_meta = select_universe()
                last_universe = cycle_start
                print(json.dumps({
                    "kind": "microstructure_universe", "authorization": AUTHORIZATION, "liveTrading": False,
                    **universe_meta, "symbols": [x["symbol"] for x in universe],
                    "quoteVolumes": {x["symbol"]: x["quoteVolume24h"] for x in universe},
                }, separators=(",", ":")), flush=True)
                if not universe:
                    raise RuntimeError("empty eligible universe")

            ts_ms = int(time.time() * 1000)
            rows = []
            errors = []
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {pool.submit(snapshot_symbol, item, ts_ms): item["symbol"] for item in universe}
                for fut in as_completed(futures):
                    symbol = futures[fut]
                    try:
                        rows.append(fut.result())
                    except Exception as exc:
                        errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})

            rows.sort(key=lambda x: x["symbol"])
            store.insert_many(rows)
            store.checkpoint()
            today = now_utc().date().isoformat()
            # Rewrite the current daily partition every 10 cycles (~10m default),
            # or immediately on first successful cycle.
            if rows and (last_export_day != today or cycles % 10 == 0):
                store.export_day(today)
                last_export_day = today
            cycles += 1
            total_errors += len(errors)
            consecutive_empty = 0 if rows else consecutive_empty + 1
            stats = store.stats()
            health = {
                "authorization": AUTHORIZATION,
                "liveTrading": False,
                "healthy": bool(rows) and consecutive_empty < 3,
                "generatedAt": iso_now(),
                "cycles": cycles,
                "topN": TOP_N,
                "universe": [x["symbol"] for x in universe],
                "universeMeta": universe_meta,
                "rowsThisCycle": len(rows),
                "errorsThisCycle": len(errors),
                "errorsSample": errors[:10],
                "totalErrors": total_errors,
                "consecutiveEmptyCycles": consecutive_empty,
                **stats,
            }
            write_health(health)
            print(json.dumps({"kind": "microstructure_runtime", **health}, separators=(",", ":")), flush=True)
        except Exception as exc:
            total_errors += 1
            consecutive_empty += 1
            health = {
                "authorization": AUTHORIZATION,
                "liveTrading": False,
                "healthy": False,
                "generatedAt": iso_now(),
                "cycles": cycles,
                "topN": TOP_N,
                "universe": [x["symbol"] for x in universe],
                "error": f"{type(exc).__name__}: {exc}",
                "totalErrors": total_errors,
                "consecutiveEmptyCycles": consecutive_empty,
                **store.stats(),
            }
            write_health(health)
            print(json.dumps({"kind": "microstructure_runtime_error", **health}, separators=(",", ":")), flush=True)

        elapsed = time.monotonic() - cycle_start
        stop_event.wait(max(1.0, POLL_SECONDS - elapsed))


def shutdown(*_):
    stop_event.set()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    main()
