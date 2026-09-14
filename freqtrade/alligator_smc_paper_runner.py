#!/usr/bin/env python3
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

REVISION = "TST_ALLIGATOR_SMC_V2_PAPER_R1"
SYMBOLS = [s.strip().upper() for s in os.getenv("ALLIGATOR_SMC_PAPER_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip()]
POLL_SECONDS = max(20, int(os.getenv("ALLIGATOR_SMC_PAPER_POLL_SEC", "60")))
STATE_PATH = Path(os.getenv("ALLIGATOR_SMC_PAPER_STATE_PATH", "/data/alligator_smc_paper_state.json"))
EVENTS_PATH = Path(os.getenv("ALLIGATOR_SMC_PAPER_EVENTS_PATH", "/data/alligator_smc_paper_events.jsonl"))
START_BALANCE = float(os.getenv("ALLIGATOR_SMC_PAPER_START_BALANCE", "20.08"))
ORDER_NOTIONAL = float(os.getenv("ALLIGATOR_SMC_PAPER_ORDER_USDT", "5.5"))
MAX_NOTIONAL = float(os.getenv("ALLIGATOR_SMC_PAPER_MAX_POSITION_USDT", "7"))
MAX_RISK_USD = float(os.getenv("ALLIGATOR_SMC_PAPER_MAX_RISK_USDT", "0.20"))
DAILY_LOSS_CAP = float(os.getenv("ALLIGATOR_SMC_PAPER_DAILY_LOSS_CAP_USDT", "0.50"))
FEE_RATE = float(os.getenv("ALLIGATOR_SMC_PAPER_FEE_RATE", "0.001"))
MIN_L2_BID_SHARE = float(os.getenv("ALLIGATOR_SMC_PAPER_MIN_L2_BID_SHARE", "0.52"))
MIN_VISIBLE_DEPTH = float(os.getenv("ALLIGATOR_SMC_PAPER_MIN_VISIBLE_DEPTH_USDT", "25000"))
MAX_SPREAD_BPS = float(os.getenv("ALLIGATOR_SMC_PAPER_MAX_SPREAD_BPS", "20"))
MIN_TAKER_BUY = float(os.getenv("ALLIGATOR_SMC_PAPER_MIN_TAKER_BUY_SHARE", "0.56"))
MIN_REL_VOLUME = float(os.getenv("ALLIGATOR_SMC_PAPER_MIN_RELATIVE_VOLUME", "1.20"))
BASE_URLS = ["https://data-api.binance.vision/api/v3", "https://api.binance.com/api/v3"]


def log(msg):
    print(f"[alligator-paper] {datetime.now(timezone.utc).isoformat()} {msg}", flush=True)


def atomic_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def append_event(payload: dict):
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def default_state():
    return {
        "revision": REVISION,
        "balanceUSDT": START_BALANCE,
        "realizedPnlUSDT": 0.0,
        "dailyRealizedPnlUSDT": 0.0,
        "dayUTC": datetime.now(timezone.utc).date().isoformat(),
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "openPosition": None,
        "lastProcessedCloseMs": {},
        "startupNotifiedRevision": None,
        "lastScanAt": None,
        "lastError": None,
    }


def load_state():
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        base = default_state()
        base.update(data)
        return base
    except Exception:
        return default_state()


def telegram(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    payload = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
    req = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "tst-alligator-paper/1.0"},
    )
    try:
        with urlopen(req, timeout=10) as r:
            r.read()
    except Exception as exc:
        log(f"telegram warning {type(exc).__name__}: {exc}")


def public_json(path: str, params: dict):
    query = urlencode(params)
    last_exc = None
    for base in BASE_URLS:
        url = f"{base}{path}?{query}"
        try:
            req = Request(url, headers={"User-Agent": "tst-alligator-paper/1.0", "Accept": "application/json"})
            with urlopen(req, timeout=12) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP_{r.status}")
                return json.loads(r.read().decode())
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"BINANCE_PUBLIC_UNAVAILABLE:{type(last_exc).__name__}:{last_exc}")


def closed_klines(symbol: str, interval: str, limit: int):
    rows = public_json("/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    now_ms = int(time.time() * 1000)
    return [r for r in rows if int(r[6]) < now_ms]


def dataframe_from_klines(rows):
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(
        {
            "open_time": [int(x[0]) for x in rows],
            "open": [float(x[1]) for x in rows],
            "high": [float(x[2]) for x in rows],
            "low": [float(x[3]) for x in rows],
            "close": [float(x[4]) for x in rows],
            "volume": [float(x[5]) for x in rows],
            "close_time": [int(x[6]) for x in rows],
            "quote_volume": [float(x[7]) for x in rows],
            "taker_buy_quote": [float(x[10]) for x in rows],
        }
    )


def smma(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def psar_series(df: pd.DataFrame, step: float = 0.02, maximum: float = 0.2) -> pd.Series:
    highs = df["high"].astype(float).tolist()
    lows = df["low"].astype(float).tolist()
    n = len(highs)
    if n == 0:
        return pd.Series(dtype="float64", index=df.index)
    if n == 1:
        return pd.Series([lows[0]], index=df.index, dtype="float64")
    out = [0.0] * n
    bull = True
    af = step
    ep = highs[0]
    out[0] = lows[0]
    for i in range(1, n):
        psar = out[i - 1] + af * (ep - out[i - 1])
        if bull:
            psar = min(psar, lows[i - 1])
            if i > 1:
                psar = min(psar, lows[i - 2])
            if lows[i] < psar:
                bull = False
                psar = ep
                ep = lows[i]
                af = step
            elif highs[i] > ep:
                ep = highs[i]
                af = min(maximum, af + step)
        else:
            psar = max(psar, highs[i - 1])
            if i > 1:
                psar = max(psar, highs[i - 2])
            if highs[i] > psar:
                bull = True
                psar = ep
                ep = highs[i]
                af = step
            elif lows[i] < ep:
                ep = lows[i]
                af = min(maximum, af + step)
        out[i] = psar
    return pd.Series(out, index=df.index, dtype="float64")


def structure_context(df: pd.DataFrame, i: int, atr: pd.Series, relvol: pd.Series) -> dict:
    opens = df["open"].astype(float).to_numpy()
    highs = df["high"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    closes = df["close"].astype(float).to_numpy()
    atrs = atr.astype(float).to_numpy()
    relvols = relvol.astype(float).to_numpy()

    demand_low = None
    demand_high = None
    demand_seed_idx = -10_000
    last_sweep_idx = -10_000
    last_sweep_low = None
    last_shift_idx = -10_000

    for j in range(i + 1):
        atr_now = atrs[j]
        zone_expired = demand_low is not None and j - demand_seed_idx > 96
        zone_invalidated = demand_low is not None and closes[j] < demand_low
        if zone_expired or zone_invalidated:
            demand_low = None
            demand_high = None
            demand_seed_idx = -10_000
            last_sweep_idx = -10_000
            last_sweep_low = None
            last_shift_idx = -10_000

        if j < 12 or not pd.notna(atr_now) or atr_now <= 0:
            continue

        prior_low = float(lows[j - 12:j].min())
        prior_high = float(highs[j - 12:j].max())

        if demand_low is not None:
            touched_demand = lows[j] <= demand_high * 1.002 and closes[j] > demand_low
            sweep_now = lows[j] < prior_low and closes[j] > prior_low and touched_demand
            if sweep_now:
                last_sweep_idx = j
                last_sweep_low = float(lows[j])

            if 0 < j - last_sweep_idx <= 8 and closes[j] > prior_high:
                last_shift_idx = j

        body = closes[j] - opens[j]
        displaced = (
            demand_low is None
            and closes[j] > prior_high
            and body >= 0.80 * atr_now
            and pd.notna(relvols[j])
            and relvols[j] >= 1.10
            and j >= 1
            and closes[j - 1] < opens[j - 1]
        )
        if displaced:
            demand_low = float(lows[j - 1])
            demand_high = float(opens[j - 1])
            demand_seed_idx = j
            last_sweep_idx = -10_000
            last_sweep_low = None
            last_shift_idx = -10_000

    atr_now = float(atrs[i]) if pd.notna(atrs[i]) else 0.0
    if demand_low is None or demand_high is None or atr_now <= 0:
        return {
            "liquiditySweepConfirmed": False,
            "marketStructureConfirmed": False,
            "demandLocationConfirmed": False,
            "roomFor2R": False,
            "demandZoneLow": 0.0,
            "demandZoneHigh": 0.0,
            "sweepLow": 0.0,
            "structureStop": 0.0,
            "structureRiskPct": 999.0,
        }

    entry = float(closes[i])
    liquidity_sweep_confirmed = 0 <= i - last_sweep_idx <= 8
    market_structure_confirmed = 0 <= i - last_shift_idx <= 6
    distance_from_zone = entry - demand_high
    demand_location_confirmed = entry >= demand_high and distance_from_zone >= 0 and distance_from_zone <= 3.0 * atr_now

    invalidation_low = min(demand_low, last_sweep_low if last_sweep_low is not None else demand_low)
    structure_stop = max(0.0, invalidation_low - 0.20 * atr_now)
    risk = entry - structure_stop
    risk_pct = risk / entry if entry > 0 and risk > 0 else 999.0

    prior_48_high = float(highs[max(0, i - 48):i].max()) if i > 0 else entry
    overhead = prior_48_high - entry
    room_for_2r = bool(risk > 0 and risk_pct <= 0.03 and (prior_48_high <= entry or overhead >= 2.0 * risk))
    return {
        "liquiditySweepConfirmed": bool(liquidity_sweep_confirmed),
        "marketStructureConfirmed": bool(market_structure_confirmed),
        "demandLocationConfirmed": bool(demand_location_confirmed),
        "roomFor2R": room_for_2r,
        "demandZoneLow": float(demand_low),
        "demandZoneHigh": float(demand_high),
        "sweepLow": float(last_sweep_low or 0.0),
        "structureStop": float(structure_stop),
        "structureRiskPct": float(risk_pct),
    }


def feature_snapshot(symbol: str) -> dict:
    rows = closed_klines(symbol, "15m", 420)
    if len(rows) < 360:
        raise RuntimeError(f"{symbol}:HISTORY_{len(rows)}")
    df = dataframe_from_klines(rows)
    i = len(df) - 1
    prev = i - 1
    close = df["close"]
    volume = df["volume"]
    jaw = smma(close, 13)
    teeth = smma(close, 8)
    lips = smma(close, 5)
    spread = (lips - jaw) / close
    ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    ema200 = close.ewm(span=200, adjust=False, min_periods=200).mean()
    htf_proxy = close.ewm(span=320, adjust=False, min_periods=320).mean()
    atr = atr_series(df)
    psar = psar_series(df)
    relvol = volume / volume.rolling(20).mean().shift(1)

    alligator = bool(
        pd.notna(jaw.iloc[i])
        and lips.iloc[i] > teeth.iloc[i] > jaw.iloc[i]
        and lips.iloc[i] > lips.iloc[prev]
        and teeth.iloc[i] > teeth.iloc[prev]
        and jaw.iloc[i] > jaw.iloc[prev]
        and spread.iloc[i] > spread.iloc[prev]
    )
    macd_ok = bool(pd.notna(macd_signal.iloc[i]) and macd.iloc[i] > macd_signal.iloc[i])
    sar_ok = bool(pd.notna(psar.iloc[i]) and psar.iloc[i] < close.iloc[i])
    vol_ok = bool(pd.notna(relvol.iloc[i]) and relvol.iloc[i] >= MIN_REL_VOLUME)
    trend_ok = bool(pd.notna(ema200.iloc[i]) and close.iloc[i] > ema200.iloc[i])
    htf_proxy_ok = bool(
        pd.notna(htf_proxy.iloc[i])
        and pd.notna(htf_proxy.iloc[prev])
        and close.iloc[i] > htf_proxy.iloc[i]
        and htf_proxy.iloc[i] > htf_proxy.iloc[prev]
    )
    structure = structure_context(df, i, atr, relvol)

    trend_exit = bool(
        lips.iloc[i] < teeth.iloc[i]
        or macd.iloc[i] < macd_signal.iloc[i]
        or psar.iloc[i] > close.iloc[i]
        or close.iloc[i] < htf_proxy.iloc[i]
    )

    reasons = []
    checks = {
        "ALLIGATOR_NOT_OPEN": alligator,
        "MACD_NOT_CONFIRMED": macd_ok,
        "SAR_NOT_CONFIRMED": sar_ok,
        "VOLUME_NOT_CONFIRMED": vol_ok,
        "EMA200_TREND_BLOCK": trend_ok,
        "HTF_PROXY_BLOCK": htf_proxy_ok,
        "NO_DEMAND_LIQUIDITY_SWEEP": structure["liquiditySweepConfirmed"],
        "NO_BULLISH_MARKET_SHIFT": structure["marketStructureConfirmed"],
        "BAD_DEMAND_LOCATION": structure["demandLocationConfirmed"],
        "NO_2R_ROOM": structure["roomFor2R"],
        "STRUCTURE_STOP_TOO_WIDE": 0 < structure["structureRiskPct"] <= 0.03,
    }
    for reason, ok in checks.items():
        if not ok:
            reasons.append(reason)

    return {
        "symbol": symbol,
        "barCloseMs": int(df["close_time"].iloc[i]),
        "barHigh": float(df["high"].iloc[i]),
        "barLow": float(df["low"].iloc[i]),
        "entry": float(close.iloc[i]),
        "alligatorTrigger": alligator,
        "macdConfirmed": macd_ok,
        "sarConfirmed": sar_ok,
        "volumeConfirmed": vol_ok,
        "trendConfirmed": trend_ok,
        "htfProxyConfirmed": htf_proxy_ok,
        "relativeVolumeCore": float(relvol.iloc[i]) if pd.notna(relvol.iloc[i]) else 0.0,
        "trendExit": trend_exit,
        "coreReasons": reasons,
        **structure,
    }


def market_confirmations(symbol: str) -> dict:
    depth = public_json("/depth", {"symbol": symbol, "limit": 20})
    rows15 = closed_klines(symbol, "15m", 25)
    rows4h = closed_klines(symbol, "4h", 25)
    btc = rows15 if symbol == "BTCUSDT" else closed_klines("BTCUSDT", "15m", 210)
    if len(rows15) < 21 or len(rows4h) < 20 or len(btc) < 200:
        raise RuntimeError("MARKET_DATA_INCOMPLETE")

    bids = [(float(p), float(q)) for p, q in depth.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in depth.get("asks", [])]
    if not bids or not asks:
        raise RuntimeError("ORDERBOOK_EMPTY")
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    spread_bps = ((best_ask - best_bid) / mid) * 10000 if mid > 0 else 999999.0
    bid_depth = sum(p * q for p, q in bids)
    ask_depth = sum(p * q for p, q in asks)
    total_depth = bid_depth + ask_depth
    bid_share = bid_depth / total_depth if total_depth > 0 else 0.0
    visible_depth = min(bid_depth, ask_depth)

    last = rows15[-1]
    quote_volume = float(last[7])
    taker_share = float(last[10]) / quote_volume if quote_volume > 0 else 0.0
    prior = [float(x[7]) for x in rows15[-21:-1]]
    relative_volume = quote_volume / (sum(prior) / len(prior)) if prior and sum(prior) > 0 else 0.0

    btc_closes = pd.Series([float(x[4]) for x in btc], dtype="float64")
    btc_ema200 = btc_closes.ewm(span=200, adjust=False, min_periods=200).mean()
    btc_regime = bool(pd.notna(btc_ema200.iloc[-1]) and btc_closes.iloc[-1] > btc_ema200.iloc[-1])

    htf_closes = pd.Series([float(x[4]) for x in rows4h], dtype="float64")
    htf_ema20 = htf_closes.ewm(span=20, adjust=False, min_periods=20).mean()
    htf_bias = bool(
        len(htf_ema20) >= 2
        and pd.notna(htf_ema20.iloc[-1])
        and pd.notna(htf_ema20.iloc[-2])
        and htf_closes.iloc[-1] > htf_ema20.iloc[-1]
        and htf_ema20.iloc[-1] > htf_ema20.iloc[-2]
    )
    return {
        "l2Confirmed": bid_share >= MIN_L2_BID_SHARE,
        "btcRegimeOk": btc_regime,
        "liquidityOk": spread_bps <= MAX_SPREAD_BPS and visible_depth >= MIN_VISIBLE_DEPTH,
        "htfBiasOk": htf_bias,
        "takerBuyShare": taker_share,
        "relativeVolume": relative_volume,
        "spreadBps": spread_bps,
        "l2BidShare": bid_share,
        "visibleDepthUSDT": visible_depth,
    }


def maybe_reset_day(state):
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("dayUTC") != today:
        state["dayUTC"] = today
        state["dailyRealizedPnlUSDT"] = 0.0


def close_position(state, snap, exit_price: float, reason: str):
    pos = state["openPosition"]
    entry = float(pos["entry"])
    qty = float(pos["qty"])
    notional = float(pos["notionalUSDT"])
    gross = qty * (exit_price - entry)
    entry_fee = notional * FEE_RATE
    exit_fee = qty * exit_price * FEE_RATE
    pnl = gross - entry_fee - exit_fee
    state["balanceUSDT"] = float(state["balanceUSDT"]) + pnl
    state["realizedPnlUSDT"] = float(state["realizedPnlUSDT"]) + pnl
    state["dailyRealizedPnlUSDT"] = float(state["dailyRealizedPnlUSDT"]) + pnl
    state["trades"] = int(state["trades"]) + 1
    if pnl > 0:
        state["wins"] = int(state["wins"]) + 1
    else:
        state["losses"] = int(state["losses"]) + 1
    event = {
        "type": "PAPER_CLOSE",
        "revision": REVISION,
        "time": datetime.now(timezone.utc).isoformat(),
        "symbol": pos["symbol"],
        "entry": entry,
        "exit": exit_price,
        "reason": reason,
        "pnlUSDT": pnl,
        "grossPnlUSDT": gross,
        "feesUSDT": entry_fee + exit_fee,
        "balanceUSDT": state["balanceUSDT"],
        "trades": state["trades"],
        "wins": state["wins"],
    }
    append_event(event)
    telegram(
        f"🧪 PAPER CLOSE — {pos['symbol']}\n"
        f"Reason: {reason}\nEntry: {entry:.8g}\nExit: {exit_price:.8g}\n"
        f"PnL: {pnl:+.4f} USDT\nBalance: {state['balanceUSDT']:.4f} USDT\n"
        f"W/L: {state['wins']}/{state['losses']}"
    )
    log(f"CLOSE {pos['symbol']} reason={reason} pnl={pnl:+.4f}")
    state["openPosition"] = None


def check_exit(state, snap):
    pos = state.get("openPosition")
    if not pos or pos.get("symbol") != snap["symbol"]:
        return
    if int(snap["barCloseMs"]) <= int(pos.get("openedBarCloseMs", 0)):
        return
    low = snap["barLow"]
    high = snap["barHigh"]
    stop = float(pos["stop"])
    target = float(pos["target"])
    if low <= stop and high >= target:
        close_position(state, snap, stop, "AMBIGUOUS_BAR_STOP_FIRST")
    elif low <= stop:
        close_position(state, snap, stop, "STOP")
    elif high >= target:
        close_position(state, snap, target, "TARGET_2R")
    elif snap["trendExit"]:
        close_position(state, snap, snap["entry"], "TREND_FAILURE")


def build_candidate(snap):
    if snap["coreReasons"]:
        return None, snap["coreReasons"]
    try:
        confirms = market_confirmations(snap["symbol"])
    except Exception as exc:
        return None, [f"MARKET_DATA_ERROR:{type(exc).__name__}"]

    reasons = []
    if not confirms["l2Confirmed"]:
        reasons.append("L2_CONFIRMATION_REQUIRED")
    if not confirms["btcRegimeOk"]:
        reasons.append("BTC_REGIME_BLOCK")
    if not confirms["liquidityOk"]:
        reasons.append("LIQUIDITY_BLOCK")
    if not confirms["htfBiasOk"]:
        reasons.append("HTF_BIAS_BLOCK")
    if confirms["takerBuyShare"] < MIN_TAKER_BUY:
        reasons.append("TAKER_BUY_TOO_LOW")
    if confirms["relativeVolume"] < MIN_REL_VOLUME:
        reasons.append("RELATIVE_VOLUME_TOO_LOW")
    if reasons:
        return None, reasons

    entry = snap["entry"]
    stop = snap["structureStop"]
    risk = entry - stop
    if risk <= 0:
        return None, ["STRUCTURE_STOP_INVALID"]
    target = entry + 2.0 * risk
    risk_pct = risk / entry
    notional = min(ORDER_NOTIONAL, MAX_NOTIONAL, MAX_RISK_USD / risk_pct)
    if notional < 1.0:
        return None, ["POSITION_TOO_SMALL"]
    risk_usd = notional * risk_pct
    score = 90
    score += 2 if confirms["l2Confirmed"] else 0
    score += 2 if confirms["takerBuyShare"] >= MIN_TAKER_BUY else 0
    score += 1 if confirms["relativeVolume"] >= MIN_REL_VOLUME else 0
    score += 1 if confirms["btcRegimeOk"] else 0
    score += 1 if confirms["liquidityOk"] else 0
    score += 3 if confirms["htfBiasOk"] else 0
    return {
        "symbol": snap["symbol"],
        "entry": entry,
        "stop": stop,
        "target": target,
        "notionalUSDT": notional,
        "riskUSDT": risk_usd,
        "score": min(score, 100),
        "barCloseMs": snap["barCloseMs"],
        "relativeVolumeCore": snap["relativeVolumeCore"],
        **confirms,
    }, []


def open_position(state, candidate):
    qty = candidate["notionalUSDT"] / candidate["entry"]
    state["openPosition"] = {
        "symbol": candidate["symbol"],
        "entry": candidate["entry"],
        "stop": candidate["stop"],
        "target": candidate["target"],
        "notionalUSDT": candidate["notionalUSDT"],
        "qty": qty,
        "riskUSDT": candidate["riskUSDT"],
        "score": candidate["score"],
        "openedAt": datetime.now(timezone.utc).isoformat(),
        "openedBarCloseMs": candidate["barCloseMs"],
    }
    event = {"type": "PAPER_OPEN", "revision": REVISION, "time": datetime.now(timezone.utc).isoformat(), **candidate}
    append_event(event)
    telegram(
        f"🧪 PAPER BUY — {candidate['symbol']}\n"
        f"Strategy: TST_ALLIGATOR_SMC_V2\nScore: {candidate['score']}/100\n"
        f"Entry: {candidate['entry']:.8g}\nStop: {candidate['stop']:.8g}\n"
        f"Target: {candidate['target']:.8g} (2R)\n"
        f"Size: {candidate['notionalUSDT']:.2f} USDT\nRisk: {candidate['riskUSDT']:.3f} USDT\n"
        f"Taker buy: {candidate['takerBuyShare']*100:.1f}% | RelVol: {candidate['relativeVolume']:.2f}x"
    )
    log(f"OPEN {candidate['symbol']} entry={candidate['entry']:.8g} stop={candidate['stop']:.8g} target={candidate['target']:.8g}")


def run_once(state):
    maybe_reset_day(state)
    snapshots = {}
    new_bar_symbols = []
    for symbol in SYMBOLS:
        try:
            snap = feature_snapshot(symbol)
            snapshots[symbol] = snap
            last_ms = int(state.get("lastProcessedCloseMs", {}).get(symbol, 0) or 0)
            if snap["barCloseMs"] > last_ms:
                new_bar_symbols.append(symbol)
        except Exception as exc:
            log(f"{symbol} snapshot error {type(exc).__name__}: {exc}")

    pos = state.get("openPosition")
    if pos and pos.get("symbol") in snapshots and pos.get("symbol") in new_bar_symbols:
        check_exit(state, snapshots[pos["symbol"]])

    candidates = []
    for symbol in new_bar_symbols:
        snap = snapshots[symbol]
        state.setdefault("lastProcessedCloseMs", {})[symbol] = snap["barCloseMs"]
        candidate, reasons = build_candidate(snap)
        if candidate:
            candidates.append(candidate)
            log(f"{symbol} PASS score={candidate['score']} relvol={candidate['relativeVolume']:.2f} taker={candidate['takerBuyShare']:.3f}")
        else:
            log(f"{symbol} NO_TRADE {'|'.join(reasons[:6])}")

    if state.get("openPosition") is None and candidates:
        if float(state.get("dailyRealizedPnlUSDT", 0.0)) <= -DAILY_LOSS_CAP:
            log(f"daily loss cap blocks new entries pnl={state['dailyRealizedPnlUSDT']:.4f}")
        else:
            best = max(candidates, key=lambda c: (c["score"], c["relativeVolume"], c["takerBuyShare"]))
            open_position(state, best)

    state["lastScanAt"] = datetime.now(timezone.utc).isoformat()
    state["lastError"] = None
    atomic_json(STATE_PATH, state)


def main():
    state = load_state()
    if state.get("startupNotifiedRevision") != REVISION:
        telegram(
            "🧪 TST_ALLIGATOR_SMC_V2 PAPER ONLINE\n"
            "BTC / ETH / SOL • 15m entries • 4h bias\n"
            "Alligator + MACD + SAR + Volume + Demand Sweep + BOS\n"
            "L2 + Taker flow + BTC regime active\n"
            "REAL MONEY: OFF"
        )
        state["startupNotifiedRevision"] = REVISION
        atomic_json(STATE_PATH, state)
    log(f"ONLINE revision={REVISION} symbols={','.join(SYMBOLS)} real_money=OFF")

    while True:
        started = time.time()
        try:
            run_once(state)
        except Exception as exc:
            state["lastError"] = f"{type(exc).__name__}:{str(exc)[:200]}"
            state["lastScanAt"] = datetime.now(timezone.utc).isoformat()
            atomic_json(STATE_PATH, state)
            log(f"cycle error {type(exc).__name__}: {exc}")
        elapsed = time.time() - started
        time.sleep(max(1.0, POLL_SECONDS - elapsed))


if __name__ == "__main__":
    main()
