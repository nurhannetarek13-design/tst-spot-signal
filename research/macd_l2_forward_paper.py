#!/usr/bin/env python3
"""MACD+EMA200 + real Binance L2 forward-paper observer.

Research/PAPER ONLY. No keys, no authenticated endpoints, no orders.
Long-only BTC/ETH/SOL 15m. Fail-closed on missing market data.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
INTERVAL = "15m"
KLINE_LIMIT = 260
DEPTH_LIMIT = 100
TAKER_BUY_MIN = 0.56
RVOL_MIN = 1.50
SPREAD_BPS_MAX = 20.0
DEPTH_IMBALANCE_MIN = 0.52
ATR_MULT = 2.0
TARGET_R = 2.0
MAX_HOLD_BARS = 16  # 4 hours on 15m
PAPER_NOTIONAL_USDT = 5.5
BASE_FRICTION_RATE = 0.0012  # 0.10% fee + 2 bps slippage per side

ROOT = pathlib.Path("validation/forward/macd-l2-v1")
LATEST = ROOT / "latest.json"
STATE = ROOT / "state.json"
HISTORY = ROOT / "history.jsonl"

API_BASES = (
    "https://api.binance.com",
    "https://data-api.binance.vision",
)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def http_json(path: str, params: dict[str, Any], timeout: int = 12) -> Any:
    query = urllib.parse.urlencode(params)
    last: Exception | None = None
    for base in API_BASES:
        url = f"{base}{path}?{query}"
        req = urllib.request.Request(url, headers={"User-Agent": "tst-forward-paper/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # fail over, then fail closed
            last = e
    raise RuntimeError(f"market_data_unavailable:{type(last).__name__}:{last}")


def ema(values: list[float], n: int) -> list[float]:
    if not values:
        return []
    a = 2.0 / (n + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(a * float(v) + (1.0 - a) * out[-1])
    return out


def atr(rows: list[list[Any]], n: int = 14) -> float:
    if len(rows) < n + 1:
        raise ValueError("insufficient_atr_bars")
    tr = []
    for i in range(len(rows) - n, len(rows)):
        h = float(rows[i][2]); lo = float(rows[i][3]); prev = float(rows[i - 1][4])
        tr.append(max(h - lo, abs(h - prev), abs(lo - prev)))
    return sum(tr) / len(tr)


def closed_klines(symbol: str) -> list[list[Any]]:
    rows = http_json("/api/v3/klines", {"symbol": symbol, "interval": INTERVAL, "limit": KLINE_LIMIT})
    now_ms = int(time.time() * 1000)
    closed = [r for r in rows if int(r[6]) < now_ms]
    if len(closed) < 220:
        raise RuntimeError(f"insufficient_closed_bars:{len(closed)}")
    return closed


def indicator_snapshot(rows: list[list[Any]]) -> dict[str, float | bool | int]:
    closes = [float(r[4]) for r in rows]
    volumes = [float(r[5]) for r in rows]
    e12 = ema(closes, 12); e26 = ema(closes, 26)
    macd = [a - b for a, b in zip(e12, e26)]
    signal = ema(macd, 9)
    e200 = ema(closes, 200)
    vbase = statistics.mean(volumes[-31:-1])
    rvol = volumes[-1] / vbase if vbase > 0 else 0.0
    vol = volumes[-1]
    taker_base = float(rows[-1][9])
    taker_share = taker_base / vol if vol > 0 else 0.0
    cross = macd[-1] > signal[-1] and macd[-2] <= signal[-2]
    core = cross and macd[-1] < 0 and closes[-1] > e200[-1]
    return {
        "close": closes[-1],
        "ema200": e200[-1],
        "macd": macd[-1],
        "macdSignal": signal[-1],
        "prevMacd": macd[-2],
        "prevMacdSignal": signal[-2],
        "macdBullCross": cross,
        "coreTrigger": core,
        "relativeVolume": rvol,
        "takerBuyShare": taker_share,
        "atr14": atr(rows, 14),
        "barOpenTime": int(rows[-1][0]),
        "barCloseTime": int(rows[-1][6]),
    }


def depth_snapshot(symbol: str) -> dict[str, float]:
    book = http_json("/api/v3/depth", {"symbol": symbol, "limit": DEPTH_LIMIT})
    bids = [(float(p), float(q)) for p, q in book.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in book.get("asks", [])]
    if not bids or not asks:
        raise RuntimeError("empty_orderbook")
    bid = bids[0][0]; ask = asks[0][0]; mid = (bid + ask) / 2.0
    spread = ((ask - bid) / mid) * 10000.0 if mid > 0 else math.inf
    bid_notional = sum(p * q for p, q in bids)
    ask_notional = sum(p * q for p, q in asks)
    total = bid_notional + ask_notional
    imbalance = bid_notional / total if total > 0 else 0.0
    return {
        "bestBid": bid,
        "bestAsk": ask,
        "spreadBps": spread,
        "bidDepthNotional": bid_notional,
        "askDepthNotional": ask_notional,
        "depthImbalance": imbalance,
    }


def load_state() -> dict[str, Any]:
    try:
        x = json.loads(STATE.read_text())
        if isinstance(x, dict):
            return x
    except Exception:
        pass
    return {"version": 1, "positions": {}, "closedTrades": [], "lastProcessedBar": {}}


def save_json(path: pathlib.Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def append_history(obj: Any) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, separators=(",", ":"), sort_keys=True) + "\n")


def settle_position(symbol: str, pos: dict[str, Any], rows: list[list[Any]], state: dict[str, Any]) -> dict[str, Any] | None:
    entry_bar = int(pos["entryBarCloseTime"])
    unseen = [r for r in rows if int(r[6]) > entry_bar]
    for idx, r in enumerate(unseen, start=1):
        high = float(r[2]); low = float(r[3]); close = float(r[4])
        stop = float(pos["stop"]); target = float(pos["target"])
        reason = None; exit_px = None
        # Conservative same-bar assumption: stop wins if both touched.
        if low <= stop:
            reason = "STOP"; exit_px = stop
        elif high >= target:
            reason = "TARGET"; exit_px = target
        elif idx >= MAX_HOLD_BARS:
            reason = "MAX_HOLD"; exit_px = close
        if reason:
            entry = float(pos["entry"])
            gross = (exit_px / entry) - 1.0
            net = gross - 2.0 * BASE_FRICTION_RATE
            trade = {
                **pos,
                "exit": exit_px,
                "exitBarCloseTime": int(r[6]),
                "exitReason": reason,
                "grossReturnPct": gross * 100.0,
                "netReturnPct": net * 100.0,
                "paperPnlUSDT": PAPER_NOTIONAL_USDT * net,
                "status": "CLOSED",
            }
            state.setdefault("closedTrades", []).append(trade)
            state["closedTrades"] = state["closedTrades"][-500:]
            return trade
    return None


def main() -> None:
    state = load_state()
    positions = state.setdefault("positions", {})
    latest: dict[str, Any] = {
        "engine": "MACD_EMA200_L2_FORWARD_PAPER_V1",
        "authorization": "PAPER_ONLY",
        "liveTrading": False,
        "executorAutoEnable": False,
        "historicalL2Validated": False,
        "universe": list(SYMBOLS),
        "timeframe": INTERVAL,
        "generatedAt": utcnow().isoformat(),
        "thresholds": {
            "takerBuyShareMin": TAKER_BUY_MIN,
            "relativeVolumeMin": RVOL_MIN,
            "spreadBpsMax": SPREAD_BPS_MAX,
            "depthImbalanceMin": DEPTH_IMBALANCE_MIN,
            "atrStopMult": ATR_MULT,
            "targetR": TARGET_R,
            "maxHoldBars": MAX_HOLD_BARS,
        },
        "symbols": {},
    }

    try:
        btc_rows = closed_klines("BTCUSDT")
        btc_ind = indicator_snapshot(btc_rows)
        btc_regime = float(btc_ind["close"]) > float(btc_ind["ema200"])
    except Exception as e:
        btc_regime = False
        latest["btcRegimeError"] = str(e)

    latest["btcRegimeBullish"] = btc_regime

    for symbol in SYMBOLS:
        rec: dict[str, Any] = {"symbol": symbol, "paperSignal": False, "decision": "NO_TRADE", "reasons": []}
        try:
            rows = btc_rows if symbol == "BTCUSDT" and "btc_rows" in locals() else closed_klines(symbol)
            ind = indicator_snapshot(rows)
            dep = depth_snapshot(symbol)
            rec.update(ind); rec.update(dep)

            # Settle any existing paper position first.
            if symbol in positions:
                closed = settle_position(symbol, positions[symbol], rows, state)
                if closed:
                    rec["closedTradeThisRun"] = closed
                    positions.pop(symbol, None)

            gates = {
                "coreTrigger": bool(ind["coreTrigger"]),
                "btcRegime": bool(btc_regime),
                "takerFlow": float(ind["takerBuyShare"]) >= TAKER_BUY_MIN,
                "relativeVolume": float(ind["relativeVolume"]) >= RVOL_MIN,
                "spread": float(dep["spreadBps"]) <= SPREAD_BPS_MAX,
                "depthImbalance": float(dep["depthImbalance"]) >= DEPTH_IMBALANCE_MIN,
                "noOpenPosition": symbol not in positions,
            }
            rec["gates"] = gates
            rec["reasons"] = [k for k, ok in gates.items() if not ok]

            bar_close = int(ind["barCloseTime"])
            is_new_bar = int(state.setdefault("lastProcessedBar", {}).get(symbol, 0)) < bar_close
            rec["newClosedBar"] = is_new_bar
            if not is_new_bar:
                rec["reasons"].append("alreadyProcessedBar")

            if is_new_bar:
                state["lastProcessedBar"][symbol] = bar_close

            if is_new_bar and all(gates.values()):
                entry = float(ind["close"])
                risk = ATR_MULT * float(ind["atr14"])
                if risk <= 0:
                    rec["reasons"].append("invalidATR")
                else:
                    stop = entry - risk
                    target = entry + TARGET_R * risk
                    pos = {
                        "symbol": symbol,
                        "entry": entry,
                        "stop": stop,
                        "target": target,
                        "atr14": float(ind["atr14"]),
                        "entryBarCloseTime": bar_close,
                        "openedAt": utcnow().isoformat(),
                        "paperNotionalUSDT": PAPER_NOTIONAL_USDT,
                        "status": "OPEN",
                    }
                    positions[symbol] = pos
                    rec["paperSignal"] = True
                    rec["decision"] = "PAPER_BUY"
                    rec["openedPaperPosition"] = pos
                    rec["reasons"] = []

        except Exception as e:
            rec["decision"] = "NO_TRADE"
            rec["dataValid"] = False
            rec["reasons"] = [f"FAIL_CLOSED:{type(e).__name__}:{e}"]
        latest["symbols"][symbol] = rec

    trades = state.get("closedTrades", [])
    pnl = sum(float(t.get("paperPnlUSDT", 0.0)) for t in trades)
    wins = sum(1 for t in trades if float(t.get("paperPnlUSDT", 0.0)) > 0)
    latest["paperPortfolio"] = {
        "openPositions": positions,
        "closedTrades": len(trades),
        "wins": wins,
        "winRate": wins / len(trades) if trades else None,
        "cumulativePaperPnlUSDT": pnl,
    }
    latest["note"] = "Forward observation only. Full L2 strategy has no historical approval and cannot enable live execution."
    state["updatedAt"] = latest["generatedAt"]

    save_json(STATE, state)
    save_json(LATEST, latest)
    append_history(latest)
    print(json.dumps(latest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
