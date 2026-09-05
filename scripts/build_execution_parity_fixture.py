#!/usr/bin/env python3
import datetime as dt
import hashlib
import json
import pathlib
import time
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

MANIFEST = pathlib.Path("validation/fusion/frozen-parity-candidate.json")
OUT = pathlib.Path("validation/fusion/execution-parity-fixture.json")
DAYS = 365
WARMUP_BARS = 800
STAKE_USDT = 5.5
BASE_FEE = 0.0015
STRESS_FEE = 0.003
PARITY_SPEC_VERSION = "TST_EXECUTION_PARITY_V1"


def fetch_klines(symbol, timeframe):
    end = int(time.time() * 1000)
    start = end - DAYS * 86400000
    step = {"15m": 900000, "1h": 3600000}[timeframe]
    rows, cursor = [], start
    while cursor < end:
        qs = urllib.parse.urlencode({
            "symbol": symbol,
            "interval": timeframe,
            "limit": 1000,
            "startTime": cursor,
            "endTime": end,
        })
        req = urllib.request.Request(
            "https://data-api.binance.vision/api/v3/klines?" + qs,
            headers={"User-Agent": "tst-execution-parity/1.0"},
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            batch = json.load(r)
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1][0]) + step
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.01)

    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
    ]
    df = pd.DataFrame(rows, columns=cols)
    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("ts")[["open", "high", "low", "close", "volume", "quote_volume"]].dropna()
    if len(df) <= WARMUP_BARS + 10:
        raise RuntimeError(f"insufficient bars: {len(df)}")
    return df


def dataset_digest(df):
    parts = []
    for ts, r in df.iterrows():
        parts.append(
            f"{int(ts.timestamp()*1000)}|{r.open:.12g}|{r.high:.12g}|{r.low:.12g}|{r.close:.12g}|{r.volume:.12g}"
        )
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def ema(s, n):
    return s.ewm(span=int(n), adjust=False).mean()


def rsi_wilder(s, n=14):
    d = s.diff()
    gain = d.clip(lower=0)
    loss = -d.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def atr_pct_sma(df, n=14):
    pc = df.close.shift(1)
    tr = pd.concat([
        (df.high - df.low).abs(),
        (df.high - pc).abs(),
        (df.low - pc).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean() / df.close


def build_signal(df, family, p, leader=None):
    c = df.close
    qv_proxy = df.volume * df.close
    relvol = qv_proxy / qv_proxy.rolling(24).median().replace(0, np.nan)
    rsi = rsi_wilder(c)

    if family == "TS_MOMENTUM":
        return (
            (c > ema(c, p["emaFast"]))
            & (ema(c, p["emaFast"]) > ema(c, p["emaSlow"]))
            & (c.pct_change(p["retLookback"]) > p["retMin"])
            & atr_pct_sma(df).between(p["atrMin"], p["atrMax"])
            & (relvol >= p["relvol"])
        )

    if family == "CROSS_CRYPTO_LEAD_LAG":
        if leader is None:
            return pd.Series(False, index=df.index)
        lead = leader.reindex(df.index).ffill()
        alt3 = c.pct_change(3)
        gap = lead - alt3
        return (
            (lead >= float(p.get("leaderRetMin", 0.012)))
            & (gap >= float(p.get("gapMin", 0.008)))
            & (alt3 > float(p.get("altRetMin", -0.02)))
            & (c > ema(c, int(p.get("emaFast", 24))))
            & (relvol >= float(p.get("relvol", 0.9)))
            & rsi.between(float(p.get("rsiMin", 42)), float(p.get("rsiMax", 70)))
        )

    if family == "LIQUIDITY_REVERSAL":
        rr = c.pct_change(p["retLookback"])
        mu = rr.rolling(p["zLookback"]).mean()
        sd = rr.rolling(p["zLookback"]).std(ddof=0).replace(0, np.nan)
        z = (rr - mu) / sd
        vr = qv_proxy / qv_proxy.rolling(7 * 24).median().replace(0, np.nan)
        return (
            (z <= p["zMax"])
            & (vr <= p["volumeRatioMax"])
            & (rsi <= p["rsiMax"])
            & (c < ema(c, 24))
        )

    if family == "VOLATILITY_BREAKOUT":
        a = atr_pct_sma(df)
        rank = a.rolling(p["compressionLookback"]).rank(pct=True)
        hh = df.high.rolling(p["lookback"]).max().shift(1)
        return (
            (rank.shift(1) <= p["compressionPct"])
            & (c > hh)
            & (relvol >= p["relvol"])
            & rsi.between(p["rsiMin"], p["rsiMax"])
        )

    if family == "TREND_BREAKOUT":
        f = ema(c, p["fast"])
        s = ema(c, p["slow"])
        hh = df.high.rolling(p["lookback"]).max().shift(1)
        return (f > s) & (c > hh) & (relvol >= p["relvol"]) & rsi.between(52, 72)

    if family == "MEAN_REVERSION":
        mid = c.rolling(20).mean()
        sd = c.rolling(20).std(ddof=0)
        lower = mid - p["bb"] * sd
        return (c < lower) & (rsi <= p["rsi_in"]) & (relvol >= 0.75)

    if family == "VOLATILITY_MOMENTUM":
        hh = df.high.rolling(p["lookback"]).max().shift(1)
        e20 = ema(c, 20)
        return (
            (c > hh)
            & (relvol >= p["relvol"])
            & rsi.between(p["rsi_min"], 74)
            & (c > e20)
        )

    raise ValueError(f"unsupported family {family}")


def build_leader(timeframe):
    if timeframe != "1h":
        return None
    series = []
    for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        adf = fetch_klines(symbol, timeframe)
        series.append(adf.close.pct_change(3).rename(symbol))
    return pd.concat(series, axis=1).mean(axis=1)


def simulate(df, signal, p, fee):
    hold = int(p.get("holdBars", 0))
    sl = float(p.get("sl", 0.03))
    tp = float(p.get("tp", 0.06))
    trades = []
    i = WARMUP_BARS

    while i < len(df) - 1:
        if not bool(signal.iloc[i]):
            i += 1
            continue

        entry_i = i + 1
        entry_ts = df.index[entry_i]
        entry = float(df.open.iloc[entry_i])
        stop = entry * (1 - sl)
        target = entry * (1 + tp)
        exit_i = min(len(df) - 1, entry_i + max(hold, 1))
        exit_price = float(df.close.iloc[exit_i])
        reason = "TIME"

        for j in range(entry_i, exit_i + 1):
            lo = float(df.low.iloc[j])
            hi = float(df.high.iloc[j])
            hit_sl = lo <= stop
            hit_tp = hi >= target
            if hit_sl and hit_tp:
                exit_i, exit_price, reason = j, stop, "SL_AMBIGUOUS_CONSERVATIVE"
                break
            if hit_sl:
                exit_i, exit_price, reason = j, stop, "SL"
                break
            if hit_tp:
                exit_i, exit_price, reason = j, target, "TP"
                break

        qty = STAKE_USDT / entry
        gross = qty * (exit_price - entry)
        fees = STAKE_USDT * fee + qty * exit_price * fee
        pnl = gross - fees
        trades.append({
            "signalTs": df.index[i].isoformat(),
            "entryTs": entry_ts.isoformat(),
            "exitTs": df.index[exit_i].isoformat(),
            "entryPrice": entry,
            "exitPrice": exit_price,
            "reason": reason,
            "pnlUSDT": pnl,
        })
        i = exit_i + 1

    return trades


def metrics(trades):
    if not trades:
        return {"trades": 0, "wins": 0, "winRate": 0.0, "profitFactor": 0.0, "expectancyUSDT": 0.0, "netPnlUSDT": 0.0}
    pnl = np.asarray([t["pnlUSDT"] for t in trades], dtype=float)
    gp = pnl[pnl > 0].sum() if np.any(pnl > 0) else 0.0
    gl = -pnl[pnl < 0].sum() if np.any(pnl < 0) else 0.0
    return {
        "trades": int(len(pnl)),
        "wins": int((pnl > 0).sum()),
        "winRate": float((pnl > 0).mean()),
        "profitFactor": float(gp / gl) if gl > 0 else (999.0 if gp > 0 else 0.0),
        "expectancyUSDT": float(pnl.mean()),
        "netPnlUSDT": float(pnl.sum()),
    }


m = json.loads(MANIFEST.read_text())
if not m.get("candidateFingerprint"):
    out = {
        "paritySpecVersion": PARITY_SPEC_VERSION,
        "status": "NO_CANDIDATE",
        "candidateFingerprint": None,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
else:
    df = fetch_klines(m["symbol"], m["timeframe"])
    leader = build_leader(m["timeframe"]) if m["family"] == "CROSS_CRYPTO_LEAD_LAG" else None
    signal = build_signal(df, m["family"], m["params"], leader).fillna(False)
    signal.iloc[:WARMUP_BARS] = False
    base_trades = simulate(df, signal, m["params"], BASE_FEE)
    stress_trades = simulate(df, signal, m["params"], STRESS_FEE)
    signal_rows = [
        {"signalTs": ts.isoformat(), "nextBarTs": df.index[i + 1].isoformat()}
        for i, ts in enumerate(df.index[:-1])
        if i >= WARMUP_BARS and bool(signal.iloc[i])
    ]
    out = {
        "paritySpecVersion": PARITY_SPEC_VERSION,
        "status": "READY",
        "candidateId": m.get("candidateId"),
        "candidateFingerprint": m.get("candidateFingerprint"),
        "symbol": m.get("symbol"),
        "family": m.get("family"),
        "timeframe": m.get("timeframe"),
        "params": m.get("params"),
        "dataset": {
            "bars": int(len(df)),
            "firstTs": df.index[0].isoformat(),
            "lastTs": df.index[-1].isoformat(),
            "sha256": dataset_digest(df),
        },
        "executionAssumptions": {
            "warmupBars": WARMUP_BARS,
            "entry": "NEXT_BAR_OPEN",
            "positionMode": "LONG_ONLY_ONE_AT_A_TIME",
            "reentry": "ALLOWED_AFTER_ACTUAL_EXIT",
            "stopTakeProfit": "INTRABAR_HIGH_LOW",
            "sameBarSlTpTieBreak": "STOP_FIRST_CONSERVATIVE",
            "timeExit": "CLOSE_AT_ENTRY_PLUS_HOLDBARS",
            "stakeUSDT": STAKE_USDT,
            "baseFeePerSide": BASE_FEE,
            "stressFeePerSide": STRESS_FEE,
            "slippage": 0.0,
            "relativeVolume": "BASE_VOLUME_X_CLOSE_OVER_24BAR_MEDIAN",
            "ema": "PANDAS_EWM_ADJUST_FALSE",
            "rsi": "WILDER_EWM_ALPHA_1_OVER_14",
            "atr": "14_BAR_SIMPLE_MEAN_TRUE_RANGE_OVER_CLOSE",
        },
        "signals": {
            "count": len(signal_rows),
            "first50": signal_rows[:50],
        },
        "base": metrics(base_trades),
        "stress2x": metrics(stress_trades),
        "baseTradesFirst50": base_trades[:50],
        "stressTradesFirst50": stress_trades[:50],
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
    }

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(out, indent=2))
print(json.dumps(out, indent=2))
