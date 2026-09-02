#!/usr/bin/env python3
import datetime as dt
import itertools
import json
import math
import pathlib
import time
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd
import vectorbt as vbt

SYMBOLS = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","LINKUSDT","AVAXUSDT","DOTUSDT"]
INTERVAL = "15m"
DAYS = 120
INIT_CASH = 20.08
STAKE_USDT = 5.5
BASE_FEE = 0.001
STRESS_FEE = 0.002
STRATEGY_ID = "TST_DISCOVERY_VECTORBT_V1"
OUT = pathlib.Path("validation/fusion/vectorbt-latest.json")

def fetch_klines(symbol, days=DAYS):
    now = int(time.time() * 1000)
    start = now - days * 24 * 60 * 60 * 1000
    rows = []
    cursor = start
    while cursor < now:
        qs = urllib.parse.urlencode({
            "symbol": symbol,
            "interval": INTERVAL,
            "limit": 1000,
            "startTime": cursor,
            "endTime": now,
        })
        req = urllib.request.Request(
            "https://data-api.binance.vision/api/v3/klines?" + qs,
            headers={"User-Agent": "tst-vectorbt-discovery/1.0"},
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            batch = json.load(r)
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1][0]) + 15 * 60 * 1000
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.02)
    if len(rows) < 2000:
        raise RuntimeError(f"{symbol}: insufficient candles {len(rows)}")
    df = pd.DataFrame(rows, columns=[
        "open_time","open","high","low","close","volume","close_time","quote_volume",
        "trades","taker_base","taker_quote","ignore"
    ])
    for c in ["open","high","low","close","volume","quote_volume","taker_quote"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("timestamp")[["open","high","low","close","volume","quote_volume","taker_quote"]].dropna()

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    d = s.diff()
    gain = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def candidate_grid():
    out = []
    for fast, slow, lookback, relvol, sl, tp in itertools.product(
        [20, 30], [60], [20, 40], [1.0, 1.3], [0.015, 0.02], [0.04]
    ):
        out.append({"family":"TREND_BREAKOUT","params":{"fast":fast,"slow":slow,"lookback":lookback,"relvol":relvol,"sl":sl,"tp":tp}})
    for bb, rsi_in, rsi_out, sl, tp in itertools.product(
        [1.6, 2.0], [28, 34], [50, 55], [0.015, 0.02], [0.025]
    ):
        out.append({"family":"MEAN_REVERSION","params":{"bb":bb,"rsi_in":rsi_in,"rsi_out":rsi_out,"sl":sl,"tp":tp}})
    for lookback, relvol, rsi_min, sl, tp in itertools.product(
        [12, 20], [1.4, 1.8], [52, 57], [0.018], [0.04, 0.06]
    ):
        out.append({"family":"VOLATILITY_MOMENTUM","params":{"lookback":lookback,"relvol":relvol,"rsi_min":rsi_min,"sl":sl,"tp":tp}})
    return out

def signals(df, cand):
    p = cand["params"]
    c, h, v = df["close"], df["high"], df["volume"]
    r = rsi(c)
    vol_med = v.rolling(24).median()
    rel = v / vol_med.replace(0, np.nan)
    fam = cand["family"]
    if fam == "TREND_BREAKOUT":
        f, s = ema(c, p["fast"]), ema(c, p["slow"])
        hh = h.rolling(p["lookback"]).max().shift(1)
        entries = (f > s) & (c > hh) & (rel >= p["relvol"]) & r.between(52, 72)
        exits = (f < s) | (r < 45)
    elif fam == "MEAN_REVERSION":
        mid = c.rolling(20).mean()
        sd = c.rolling(20).std(ddof=0)
        lower = mid - p["bb"] * sd
        entries = (c < lower) & (r <= p["rsi_in"]) & (rel >= 0.75)
        exits = (c >= mid) | (r >= p["rsi_out"])
    else:
        hh = h.rolling(p["lookback"]).max().shift(1)
        e20 = ema(c, 20)
        entries = (c > hh) & (rel >= p["relvol"]) & (r >= p["rsi_min"]) & (r <= 74) & (c > e20)
        exits = (c < e20) | (r < 45)
    return entries.fillna(False), exits.fillna(False)

def evaluate_one(df, cand, start_i, end_i, fee):
    sub = df.iloc[start_i:end_i].copy()
    if len(sub) < 300:
        return []
    entries, exits = signals(sub, cand)
    try:
        pf = vbt.Portfolio.from_signals(
            sub["close"],
            entries,
            exits,
            init_cash=INIT_CASH,
            size=STAKE_USDT,
            size_type="value",
            fees=fee,
            sl_stop=cand["params"]["sl"],
            tp_stop=cand["params"]["tp"],
            direction="longonly",
            freq="15min",
        )
        rr = pf.trades.closed.records_readable
        if rr.empty:
            return []
        out = []
        for _, row in rr.iterrows():
            out.append({
                "exit": pd.Timestamp(row["Exit Timestamp"]).isoformat(),
                "pnl": float(row["PnL"]),
                "ret": float(row["Return"]),
            })
        return out
    except Exception as e:
        return [{"error": str(e)}]

def aggregate(trades):
    trades = [t for t in trades if "pnl" in t]
    trades.sort(key=lambda x: x["exit"])
    pnls = np.array([t["pnl"] for t in trades], dtype=float)
    n = int(len(pnls))
    if n == 0:
        return {"trades":0,"wins":0,"winRate":0.0,"netPnlUSDT":0.0,"expectancyUSDT":0.0,"profitFactor":0.0,"maxDrawdownUSDT":0.0}
    gp = float(pnls[pnls > 0].sum()) if np.any(pnls > 0) else 0.0
    gl = float(-pnls[pnls < 0].sum()) if np.any(pnls < 0) else 0.0
    eq = np.cumsum(pnls)
    peak = np.maximum.accumulate(np.r_[0.0, eq])[:-1]
    dd = peak - eq
    return {
        "trades": n,
        "wins": int((pnls > 0).sum()),
        "winRate": float((pnls > 0).mean()),
        "netPnlUSDT": float(pnls.sum()),
        "expectancyUSDT": float(pnls.mean()),
        "profitFactor": float(gp / gl) if gl > 0 else (999.0 if gp > 0 else 0.0),
        "maxDrawdownUSDT": float(max(0.0, dd.max(initial=0.0))),
    }

def score(m):
    if m["trades"] < 25:
        return -999.0 + m["trades"]
    pf = min(m["profitFactor"], 3.0)
    return 35 * m["expectancyUSDT"] + 2.5 * pf + 0.015 * m["trades"] - 0.5 * m["maxDrawdownUSDT"]

print("Downloading Binance Spot data...")
data = {s: fetch_klines(s) for s in SYMBOLS}
min_len = min(len(x) for x in data.values())
train_end = int(min_len * 0.60)
val_end = int(min_len * 0.80)
grid = candidate_grid()
ranked = []

for i, cand in enumerate(grid, 1):
    trades = []
    for symbol, df in data.items():
        offset = len(df) - min_len
        trades += evaluate_one(df, cand, offset + train_end, offset + val_end, BASE_FEE)
    m = aggregate(trades)
    ranked.append({**cand, "candidateId": f"VBT-{i:03d}", "validation": m, "score": score(m)})

ranked.sort(key=lambda x: x["score"], reverse=True)
finalists = []
for cand in ranked[:8]:
    base_trades, stress_trades = [], []
    for symbol, df in data.items():
        offset = len(df) - min_len
        base_trades += evaluate_one(df, cand, offset + val_end, offset + min_len, BASE_FEE)
        stress_trades += evaluate_one(df, cand, offset + val_end, offset + min_len, STRESS_FEE)
    base = aggregate(base_trades)
    stress = aggregate(stress_trades)
    passed = (
        base["trades"] >= 50
        and base["profitFactor"] >= 1.15
        and base["expectancyUSDT"] > 0
        and stress["profitFactor"] >= 1.0
        and stress["expectancyUSDT"] > 0
        and base["maxDrawdownUSDT"] <= 4.0
    )
    finalists.append({**cand, "holdoutBase": base, "holdoutStress2x": stress, "discoveryPass": passed})

finalists.sort(key=lambda x: (x["discoveryPass"], x["score"]), reverse=True)
selected = finalists[0] if finalists else None
status = "CANDIDATE_FOUND" if selected and selected["discoveryPass"] else "NO_DISCOVERY_PASS"

report = {
    "engine": "VECTORBT",
    "strategyId": STRATEGY_ID,
    "status": status,
    "pass": bool(selected and selected["discoveryPass"]),
    "authorization": "RESEARCH_ONLY",
    "liveTrading": False,
    "symbols": SYMBOLS,
    "timeframe": INTERVAL,
    "days": DAYS,
    "split": {"train":0.60,"validation":0.20,"finalHoldout":0.20},
    "fees": {"basePerSide":BASE_FEE,"stressPerSide":STRESS_FEE},
    "candidateCount": len(grid),
    "selected": selected,
    "finalists": finalists[:5],
    "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
