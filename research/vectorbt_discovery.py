#!/usr/bin/env python3
import datetime as dt
import itertools
import json
import pathlib
import time
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd
import vectorbt as vbt

INTERVAL = "15m"
DAYS = 120
INIT_CASH = 20.08
STAKE_USDT = 5.5
BASE_FEE = 0.001
STRESS_FEE = 0.002
MIN_QV_24H = 5_000_000
UNIVERSE_SIZE = 40
STRATEGY_ID = "TST_DISCOVERY_VECTORBT_V1"
OUT = pathlib.Path("validation/fusion/vectorbt-latest.json")

EXCLUDED_BASES = {
    "USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","AEUR","TRY","BRL","GBP","AUD",
    "USD1","RLUSD","USDE","PAXG","XAUT"
}

def api_json(path):
    req = urllib.request.Request(
        "https://data-api.binance.vision" + path,
        headers={"User-Agent": "tst-vectorbt-universe/2.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def allowed_base(base):
    if not base or base in EXCLUDED_BASES:
        return False
    if base.endswith(("UP","DOWN","BULL","BEAR")):
        return False
    if base.endswith("B") and base not in {"BNB","ARB","WBB","KUB"}:
        return False
    return True

def discover_universe():
    info = api_json("/api/v3/exchangeInfo")
    tickers = api_json("/api/v3/ticker/24hr")
    tradable = {
        x["symbol"]: x
        for x in info.get("symbols", [])
        if x.get("status") == "TRADING"
        and x.get("quoteAsset") == "USDT"
        and x.get("isSpotTradingAllowed")
    }
    rows = []
    for t in tickers:
        s = t.get("symbol", "")
        meta = tradable.get(s)
        if not meta:
            continue
        base = meta.get("baseAsset", "")
        if not allowed_base(base):
            continue
        qv = float(t.get("quoteVolume") or 0)
        if qv < MIN_QV_24H:
            continue
        rows.append({"symbol": s, "base": base, "quoteVolume24h": qv})
    rows.sort(key=lambda x: x["quoteVolume24h"], reverse=True)
    if len(rows) <= UNIVERSE_SIZE:
        return rows

    # Balanced liquidity sampling: majors/high-liquidity + mid + lower-liquid names.
    hi = rows[:15]
    mid_start = max(15, len(rows)//3)
    mid = rows[mid_start:mid_start+15]
    low_start = max(mid_start+15, (2*len(rows))//3)
    low = rows[low_start:low_start+10]

    picked, seen = [], set()
    for bucket in (hi, mid, low, rows):
        for x in bucket:
            if x["symbol"] in seen:
                continue
            picked.append(x)
            seen.add(x["symbol"])
            if len(picked) >= UNIVERSE_SIZE:
                return picked
    return picked

def fetch_klines(symbol, days=DAYS):
    now = int(time.time() * 1000)
    start = now - days * 24 * 60 * 60 * 1000
    rows, cursor = [], start
    while cursor < now:
        qs = urllib.parse.urlencode({
            "symbol": symbol,
            "interval": INTERVAL,
            "limit": 1000,
            "startTime": cursor,
            "endTime": now,
        })
        batch = api_json("/api/v3/klines?" + qs)
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1][0]) + 15 * 60 * 1000
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.015)
    if len(rows) < 2500:
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
    for fast, slow, lookback, relvol, sl in itertools.product(
        [20,30], [60], [20,40], [1.0,1.3], [0.015,0.02]
    ):
        out.append({"family":"TREND_BREAKOUT","params":{
            "fast":fast,"slow":slow,"lookback":lookback,"relvol":relvol,"sl":sl,"tp":0.04
        }})

    for bb, rsi_in, rsi_out, sl in itertools.product(
        [1.6,2.0], [28,34], [50,55], [0.015,0.02]
    ):
        out.append({"family":"MEAN_REVERSION","params":{
            "bb":bb,"rsi_in":rsi_in,"rsi_out":rsi_out,"sl":sl,"tp":0.025
        }})

    for lookback, relvol, rsi_min, tp in itertools.product(
        [12,20], [1.4,1.8], [52,57], [0.04,0.06]
    ):
        out.append({"family":"VOLATILITY_MOMENTUM","params":{
            "lookback":lookback,"relvol":relvol,"rsi_min":rsi_min,"sl":0.018,"tp":tp
        }})

    for btc_ret, alt_lag, relvol in itertools.product(
        [0.004,0.007], [0.001,0.003], [0.8,1.1]
    ):
        out.append({"family":"BTC_LEAD_LAG","params":{
            "btc_ret":btc_ret,"alt_lag":alt_lag,"relvol":relvol,"sl":0.018,"tp":0.035
        }})

    for shock, relvol, mode in itertools.product(
        [0.012,0.02], [1.6,2.0], ["CONTINUATION","REVERSAL"]
    ):
        out.append({"family":"VOLUME_ANOMALY","params":{
            "shock":shock,"relvol":relvol,"mode":mode,"sl":0.018,"tp":0.035
        }})

    return out

def signals(df, cand, btc=None):
    p = cand["params"]
    c, h, v = df["close"], df["high"], df["volume"]
    r = rsi(c)
    vol_med = v.rolling(24).median()
    rel = v / vol_med.replace(0, np.nan)
    fam = cand["family"]

    if fam == "TREND_BREAKOUT":
        f, s = ema(c, p["fast"]), ema(c, p["slow"])
        hh = h.rolling(p["lookback"]).max().shift(1)
        entries = (f > s) & (c > hh) & (rel >= p["relvol"]) & r.between(52,72)
        exits = (f < s) | (r < 45)

    elif fam == "MEAN_REVERSION":
        mid = c.rolling(20).mean()
        sd = c.rolling(20).std(ddof=0)
        lower = mid - p["bb"] * sd
        entries = (c < lower) & (r <= p["rsi_in"]) & (rel >= 0.75)
        exits = (c >= mid) | (r >= p["rsi_out"])

    elif fam == "VOLATILITY_MOMENTUM":
        hh = h.rolling(p["lookback"]).max().shift(1)
        e20 = ema(c,20)
        entries = (c > hh) & (rel >= p["relvol"]) & r.between(p["rsi_min"],74) & (c > e20)
        exits = (c < e20) | (r < 45)

    elif fam == "BTC_LEAD_LAG":
        if btc is None:
            return pd.Series(False,index=df.index), pd.Series(False,index=df.index)
        b = btc["close"].reindex(df.index).ffill()
        btc4 = b.pct_change(4)
        alt2 = c.pct_change(2)
        e50 = ema(c,50)
        entries = (
            (btc4 >= p["btc_ret"])
            & (alt2 <= p["alt_lag"])
            & (alt2 >= -0.02)
            & (rel >= p["relvol"])
            & (c > e50)
            & r.between(45,68)
        )
        exits = (c < e50) | (r > 72) | (btc4 < -0.003)

    else:
        ret4 = c.pct_change(4)
        e20 = ema(c,20)
        if p["mode"] == "CONTINUATION":
            entries = (ret4 >= p["shock"]) & (rel >= p["relvol"]) & (c > e20) & r.between(55,75)
            exits = (c < e20) | (r < 48)
        else:
            entries = (ret4 <= -p["shock"]) & (rel >= p["relvol"]) & (r <= 32)
            exits = (c >= e20) | (r >= 52)

    return entries.fillna(False), exits.fillna(False)

def evaluate_one(df, cand, start_i, end_i, fee, btc=None):
    sub = df.iloc[start_i:end_i].copy()
    if len(sub) < 300:
        return []
    btc_sub = None if btc is None else btc.reindex(sub.index).ffill()
    entries, exits = signals(sub, cand, btc_sub)
    try:
        pf = vbt.Portfolio.from_signals(
            sub["close"], entries, exits,
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
        return [{
            "exit": pd.Timestamp(row["Exit Timestamp"]).isoformat(),
            "pnl": float(row["PnL"]),
            "ret": float(row["Return"]),
        } for _, row in rr.iterrows()]
    except Exception:
        return []

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
    peak = np.maximum.accumulate(np.r_[0.0,eq])[:-1]
    dd = peak - eq
    return {
        "trades":n,
        "wins":int((pnls > 0).sum()),
        "winRate":float((pnls > 0).mean()),
        "netPnlUSDT":float(pnls.sum()),
        "expectancyUSDT":float(pnls.mean()),
        "profitFactor":float(gp/gl) if gl > 0 else (999.0 if gp > 0 else 0.0),
        "maxDrawdownUSDT":float(max(0.0,dd.max(initial=0.0))),
    }

def validation_score(m):
    if m["trades"] < 20:
        return -999 + m["trades"]
    pf = min(m["profitFactor"],3.0)
    return 50*m["expectancyUSDT"] + 3.0*pf + 0.01*m["trades"] - 0.4*m["maxDrawdownUSDT"]

universe = discover_universe()
symbols = [x["symbol"] for x in universe]
print("Universe:", symbols)

data = {}
failures = {}
for s in symbols:
    try:
        data[s] = fetch_klines(s)
    except Exception as e:
        failures[s] = str(e)

if "BTCUSDT" not in data:
    data["BTCUSDT"] = fetch_klines("BTCUSDT")

btc = data["BTCUSDT"]
grid = candidate_grid()
ranked = []

for symbol, df in data.items():
    if symbol == "BTCUSDT":
        # BTC is allowed, but BTC lead/lag family is skipped for BTC itself.
        local_grid = [x for x in grid if x["family"] != "BTC_LEAD_LAG"]
    else:
        local_grid = grid

    n = len(df)
    val_start = int(n*0.60)
    val_end = int(n*0.80)
    for i, cand in enumerate(local_grid,1):
        tr = evaluate_one(df,cand,val_start,val_end,BASE_FEE,btc=btc)
        m = aggregate(tr)
        ranked.append({
            **cand,
            "symbol":symbol,
            "candidateId":f"{symbol}-{cand['family']}-{i:03d}",
            "validation":m,
            "score":validation_score(m),
        })

ranked.sort(key=lambda x:x["score"], reverse=True)

# One finalist per symbol first, then global ranking, to avoid one coin monopolizing the shortlist.
best_by_symbol = {}
for row in ranked:
    best_by_symbol.setdefault(row["symbol"], row)
shortlist = sorted(best_by_symbol.values(), key=lambda x:x["score"], reverse=True)[:20]

finalists = []
for cand in shortlist:
    df = data[cand["symbol"]]
    n = len(df)
    hold_start = int(n*0.80)
    base = aggregate(evaluate_one(df,cand,hold_start,n,BASE_FEE,btc=btc))
    stress = aggregate(evaluate_one(df,cand,hold_start,n,STRESS_FEE,btc=btc))
    passed = (
        cand["validation"]["trades"] >= 20
        and cand["validation"]["profitFactor"] >= 1.05
        and cand["validation"]["expectancyUSDT"] > 0
        and base["trades"] >= 20
        and base["profitFactor"] >= 1.15
        and base["expectancyUSDT"] > 0
        and stress["profitFactor"] >= 1.0
        and stress["expectancyUSDT"] > 0
        and base["maxDrawdownUSDT"] <= 2.5
    )
    finalists.append({
        **cand,
        "holdoutBase":base,
        "holdoutStress2x":stress,
        "discoveryPass":passed,
    })

finalists.sort(
    key=lambda x:(x["discoveryPass"], x["holdoutStress2x"]["expectancyUSDT"], x["holdoutBase"]["profitFactor"]),
    reverse=True,
)
selected = finalists[0] if finalists else None
status = "CANDIDATE_FOUND" if selected and selected["discoveryPass"] else "NO_DISCOVERY_PASS"

report = {
    "engine":"VECTORBT",
    "strategyId":STRATEGY_ID,
    "status":status,
    "pass":bool(selected and selected["discoveryPass"]),
    "authorization":"RESEARCH_ONLY",
    "liveTrading":False,
    "universeMode":"DYNAMIC_LIQUID_USDT_SPOT",
    "universeSizeRequested":UNIVERSE_SIZE,
    "symbolsTested":sorted(data.keys()),
    "universeMeta":universe,
    "dataFailures":failures,
    "timeframe":INTERVAL,
    "days":DAYS,
    "split":{"validationStart":0.60,"finalHoldoutStart":0.80},
    "fees":{"basePerSide":BASE_FEE,"stressPerSide":STRESS_FEE},
    "candidateFamilies":sorted({x["family"] for x in grid}),
    "candidateCountPerSymbol":len(grid),
    "totalValidationRuns":len(ranked),
    "selected":selected,
    "finalists":finalists[:10],
    "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
}
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
