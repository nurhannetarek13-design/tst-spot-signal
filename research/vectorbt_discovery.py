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
SCREEN_DAYS = 30
DEEP_DAYS = 120
INIT_CASH = 20.08
STAKE_USDT = 5.5
BASE_FEE = 0.001
STRESS_FEE = 0.002
MIN_LIVE_QV_24H = 5_000_000
STRATEGY_ID = "TST_DISCOVERY_VECTORBT_V1"
OUT = pathlib.Path("validation/fusion/vectorbt-latest.json")

EXCLUDED_BASES = {
    "USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","AEUR","TRY","BRL","GBP","AUD",
    "USD1","RLUSD","USDE","PAXG","XAUT"
}

def api_json(path):
    req = urllib.request.Request(
        "https://data-api.binance.vision" + path,
        headers={"User-Agent": "tst-vectorbt-all-universe/3.0"},
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
    tickers = {x.get("symbol"): x for x in api_json("/api/v3/ticker/24hr")}
    rows = []
    for x in info.get("symbols", []):
        if x.get("status") != "TRADING" or x.get("quoteAsset") != "USDT" or not x.get("isSpotTradingAllowed"):
            continue
        base = x.get("baseAsset", "")
        if not allowed_base(base):
            continue
        t = tickers.get(x.get("symbol"), {})
        onboard = int(x.get("onboardDate") or x.get("onboardingDate") or 0)
        qv = float(t.get("quoteVolume") or 0)
        rows.append({
            "symbol": x["symbol"],
            "base": base,
            "quoteVolume24h": qv,
            "onboardDate": onboard or None,
            "listingAgeDaysMeta": ((time.time()*1000-onboard)/86400000 if onboard > 0 else None),
        })
    rows.sort(key=lambda r: r["quoteVolume24h"], reverse=True)
    return rows

def fetch_klines(symbol, days, min_bars=48):
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
        time.sleep(0.01)
    if len(rows) < min_bars:
        raise RuntimeError(f"{symbol}: insufficient candles {len(rows)}")
    df = pd.DataFrame(rows, columns=[
        "open_time","open","high","low","close","volume","close_time","quote_volume",
        "trades","taker_base","taker_quote","ignore"
    ])
    for c in ["open","high","low","close","volume","quote_volume","taker_quote"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("timestamp")[["open","high","low","close","volume","quote_volume","taker_quote"]].dropna()

def history_days(df):
    if len(df) < 2:
        return 0.0
    return float((df.index[-1] - df.index[0]).total_seconds() / 86400)

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    d = s.diff()
    gain = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def screen_grid():
    return [
        {"family":"TREND_BREAKOUT","params":{"fast":20,"slow":60,"lookback":20,"relvol":1.1,"sl":0.018,"tp":0.04}},
        {"family":"MEAN_REVERSION","params":{"bb":1.8,"rsi_in":32,"rsi_out":52,"sl":0.018,"tp":0.025}},
        {"family":"VOLATILITY_MOMENTUM","params":{"lookback":12,"relvol":1.5,"rsi_min":54,"sl":0.018,"tp":0.05}},
        {"family":"BTC_LEAD_LAG","params":{"btc_ret":0.005,"alt_lag":0.002,"relvol":0.9,"sl":0.018,"tp":0.035}},
        {"family":"VOLUME_ANOMALY","params":{"shock":0.015,"relvol":1.7,"mode":"CONTINUATION","sl":0.018,"tp":0.035}},
        {"family":"VOLUME_ANOMALY","params":{"shock":0.015,"relvol":1.7,"mode":"REVERSAL","sl":0.018,"tp":0.03}},
        {"family":"NEW_LISTING_MOMENTUM","params":{"lookback":12,"relvol":1.4,"taker":0.56,"sl":0.02,"tp":0.04}},
    ]

def deep_grid():
    out = []
    for fast, slow, lookback, relvol, sl in itertools.product(
        [20,30], [60], [20,40], [1.0,1.3], [0.015,0.02]
    ):
        out.append({"family":"TREND_BREAKOUT","params":{"fast":fast,"slow":slow,"lookback":lookback,"relvol":relvol,"sl":sl,"tp":0.04}})
    for bb, rsi_in, rsi_out, sl in itertools.product(
        [1.6,2.0], [28,34], [50,55], [0.015,0.02]
    ):
        out.append({"family":"MEAN_REVERSION","params":{"bb":bb,"rsi_in":rsi_in,"rsi_out":rsi_out,"sl":sl,"tp":0.025}})
    for lookback, relvol, rsi_min, tp in itertools.product(
        [12,20], [1.4,1.8], [52,57], [0.04,0.06]
    ):
        out.append({"family":"VOLATILITY_MOMENTUM","params":{"lookback":lookback,"relvol":relvol,"rsi_min":rsi_min,"sl":0.018,"tp":tp}})
    for btc_ret, alt_lag, relvol in itertools.product(
        [0.004,0.007], [0.001,0.003], [0.8,1.1]
    ):
        out.append({"family":"BTC_LEAD_LAG","params":{"btc_ret":btc_ret,"alt_lag":alt_lag,"relvol":relvol,"sl":0.018,"tp":0.035}})
    for shock, relvol, mode in itertools.product(
        [0.012,0.02], [1.6,2.0], ["CONTINUATION","REVERSAL"]
    ):
        out.append({"family":"VOLUME_ANOMALY","params":{"shock":shock,"relvol":relvol,"mode":mode,"sl":0.018,"tp":0.035}})
    for lookback, relvol, taker, tp in itertools.product(
        [8,12,20], [1.2,1.5,1.8], [0.55,0.60], [0.035,0.05]
    ):
        out.append({"family":"NEW_LISTING_MOMENTUM","params":{"lookback":lookback,"relvol":relvol,"taker":taker,"sl":0.02,"tp":tp}})
    return out

def signals(df, cand, btc=None):
    p = cand["params"]
    c, h, v = df["close"], df["high"], df["volume"]
    r = rsi(c)
    vol_med = v.rolling(24).median()
    rel = v / vol_med.replace(0, np.nan)
    taker_ratio = (df["taker_quote"].rolling(4).sum() / df["quote_volume"].rolling(4).sum().replace(0, np.nan)).fillna(0.5)
    fam = cand["family"]

    if fam == "TREND_BREAKOUT":
        f, s = ema(c,p["fast"]), ema(c,p["slow"])
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

    elif fam == "VOLUME_ANOMALY":
        ret4 = c.pct_change(4)
        e20 = ema(c,20)
        if p["mode"] == "CONTINUATION":
            entries = (ret4 >= p["shock"]) & (rel >= p["relvol"]) & (c > e20) & r.between(55,75)
            exits = (c < e20) | (r < 48)
        else:
            entries = (ret4 <= -p["shock"]) & (rel >= p["relvol"]) & (r <= 32)
            exits = (c >= e20) | (r >= 52)

    else:
        hh = h.rolling(p["lookback"]).max().shift(1)
        e20 = ema(c,20)
        entries = (
            (c > hh)
            & (c > df["open"])
            & (rel >= p["relvol"])
            & (taker_ratio >= p["taker"])
            & (c > e20)
            & r.between(50,78)
        )
        exits = (c < e20) | (r < 45)

    return entries.fillna(False), exits.fillna(False)

def evaluate_one(df,cand,start_i,end_i,fee,btc=None):
    sub = df.iloc[start_i:end_i].copy()
    if len(sub) < 96:
        return []
    btc_sub = None if btc is None else btc.reindex(sub.index).ffill()
    entries, exits = signals(sub,cand,btc_sub)
    try:
        pf = vbt.Portfolio.from_signals(
            sub["close"],entries,exits,
            init_cash=INIT_CASH,size=STAKE_USDT,size_type="value",fees=fee,
            sl_stop=cand["params"]["sl"],tp_stop=cand["params"]["tp"],
            direction="longonly",freq="15min",
        )
        rr = pf.trades.closed.records_readable
        if rr.empty:
            return []
        return [{"exit":pd.Timestamp(row["Exit Timestamp"]).isoformat(),"pnl":float(row["PnL"]),"ret":float(row["Return"])} for _,row in rr.iterrows()]
    except Exception:
        return []

def aggregate(trades):
    trades = [t for t in trades if "pnl" in t]
    trades.sort(key=lambda x:x["exit"])
    pnls = np.asarray([x["pnl"] for x in trades],dtype=float)
    if len(pnls)==0:
        return {"trades":0,"wins":0,"winRate":0.0,"netPnlUSDT":0.0,"expectancyUSDT":0.0,"profitFactor":0.0,"maxDrawdownUSDT":0.0}
    gp = float(pnls[pnls>0].sum()) if np.any(pnls>0) else 0.0
    gl = float(-pnls[pnls<0].sum()) if np.any(pnls<0) else 0.0
    eq = np.cumsum(pnls)
    peak = np.maximum.accumulate(np.r_[0.0,eq])[:-1]
    dd = peak-eq
    return {
        "trades":int(len(pnls)),
        "wins":int((pnls>0).sum()),
        "winRate":float((pnls>0).mean()),
        "netPnlUSDT":float(pnls.sum()),
        "expectancyUSDT":float(pnls.mean()),
        "profitFactor":float(gp/gl) if gl>0 else (999.0 if gp>0 else 0.0),
        "maxDrawdownUSDT":float(max(0.0,dd.max(initial=0.0))),
    }

def score(m):
    if m["trades"] < 3:
        return -999 + m["trades"]
    return 40*m["expectancyUSDT"] + 2.5*min(m["profitFactor"],3.0) + 0.02*m["trades"] - 0.35*m["maxDrawdownUSDT"]

universe = discover_universe()
meta = {x["symbol"]:x for x in universe}
print(f"All tradable USDT Spot symbols: {len(universe)}")

screen_data, failures = {}, {}
for row in universe:
    s = row["symbol"]
    try:
        df = fetch_klines(s,SCREEN_DAYS,min_bars=48)
        row["screenHistoryDays"] = history_days(df)
        row["isNewListing"] = bool(
            (row.get("listingAgeDaysMeta") is not None and row["listingAgeDaysMeta"] <= 30)
            or row["screenHistoryDays"] < 27
        )
        screen_data[s] = df
    except Exception as e:
        failures[s] = str(e)

if "BTCUSDT" not in screen_data:
    screen_data["BTCUSDT"] = fetch_klines("BTCUSDT",SCREEN_DAYS,min_bars=96)

btc_screen = screen_data["BTCUSDT"]
screen_rows = []

for symbol,df in screen_data.items():
    is_new = bool(meta.get(symbol,{}).get("isNewListing"))
    local = screen_grid()
    if symbol=="BTCUSDT":
        local=[x for x in local if x["family"]!="BTC_LEAD_LAG"]
    if not is_new:
        local=[x for x in local if x["family"]!="NEW_LISTING_MOMENTUM"]
    n=len(df)
    start=max(0,int(n*0.50))
    for idx,cand in enumerate(local,1):
        m=aggregate(evaluate_one(df,cand,start,n,BASE_FEE,btc=btc_screen))
        screen_rows.append({
            **cand,
            "symbol":symbol,
            "candidateId":f"SCR-{symbol}-{cand['family']}-{idx:02d}",
            "screen":m,
            "screenScore":score(m),
            "isNewListing":is_new,
            "historyDays":history_days(df),
            "quoteVolume24h":float(meta.get(symbol,{}).get("quoteVolume24h",0)),
        })

best_by_symbol={}
for x in sorted(screen_rows,key=lambda r:r["screenScore"],reverse=True):
    best_by_symbol.setdefault(x["symbol"],x)

established=[x for x in best_by_symbol.values() if not x["isNewListing"]]
new=[x for x in best_by_symbol.values() if x["isNewListing"]]
established.sort(key=lambda r:r["screenScore"],reverse=True)
new.sort(key=lambda r:r["screenScore"],reverse=True)

# Every pair is screened. Deep validation is reserved for the best screeners so the free CI job remains bounded.
deep_symbols=[x["symbol"] for x in established[:35]] + [x["symbol"] for x in new[:25]]
deep_symbols=list(dict.fromkeys(deep_symbols))

deep_data={}
for s in deep_symbols:
    try:
        deep_data[s]=fetch_klines(s,DEEP_DAYS,min_bars=96)
    except Exception:
        deep_data[s]=screen_data[s]

btc_deep=deep_data.get("BTCUSDT",screen_data["BTCUSDT"])
grid=deep_grid()
ranked=[]

for symbol,df in deep_data.items():
    is_new=bool(meta.get(symbol,{}).get("isNewListing"))
    local=grid
    if symbol=="BTCUSDT":
        local=[x for x in local if x["family"]!="BTC_LEAD_LAG"]
    if not is_new:
        local=[x for x in local if x["family"]!="NEW_LISTING_MOMENTUM"]
    n=len(df)
    v0=int(n*0.60)
    v1=int(n*0.80)
    for idx,cand in enumerate(local,1):
        m=aggregate(evaluate_one(df,cand,v0,v1,BASE_FEE,btc=btc_deep))
        ranked.append({
            **cand,
            "symbol":symbol,
            "candidateId":f"VBT-{symbol}-{cand['family']}-{idx:03d}",
            "validation":m,
            "score":score(m),
            "isNewListing":is_new,
            "historyDays":history_days(df),
            "quoteVolume24h":float(meta.get(symbol,{}).get("quoteVolume24h",0)),
        })

best_deep={}
for row in sorted(ranked,key=lambda r:r["score"],reverse=True):
    best_deep.setdefault(row["symbol"],row)

finalists=[]
for cand in best_deep.values():
    df=deep_data[cand["symbol"]]
    n=len(df)
    h0=int(n*0.80)
    base=aggregate(evaluate_one(df,cand,h0,n,BASE_FEE,btc=btc_deep))
    stress=aggregate(evaluate_one(df,cand,h0,n,STRESS_FEE,btc=btc_deep))
    live_eligible=(
        cand["historyDays"] >= 30
        and cand["quoteVolume24h"] >= MIN_LIVE_QV_24H
        and not cand["isNewListing"]
    )
    discovery_pass=(
        live_eligible
        and cand["validation"]["trades"] >= 20
        and cand["validation"]["profitFactor"] >= 1.05
        and cand["validation"]["expectancyUSDT"] > 0
        and base["trades"] >= 20
        and base["profitFactor"] >= 1.15
        and base["expectancyUSDT"] > 0
        and stress["profitFactor"] >= 1.0
        and stress["expectancyUSDT"] > 0
        and base["maxDrawdownUSDT"] <= 2.5
    )
    paper_candidate=(
        base["trades"] >= 5
        and base["expectancyUSDT"] > 0
        and stress["expectancyUSDT"] > 0
        and stress["profitFactor"] >= 0.9
    )
    finalists.append({
        **cand,
        "holdoutBase":base,
        "holdoutStress2x":stress,
        "liveEligible":live_eligible,
        "paperCandidate":paper_candidate,
        "discoveryPass":discovery_pass,
    })

finalists.sort(
    key=lambda x:(x["discoveryPass"],x["paperCandidate"],x["holdoutStress2x"]["expectancyUSDT"],x["holdoutBase"]["profitFactor"]),
    reverse=True,
)
selected_live=next((x for x in finalists if x["discoveryPass"]),None)
selected_paper=next((x for x in finalists if x["paperCandidate"]),None)
new_watch=[x for x in finalists if x["isNewListing"]][:15]

report={
    "engine":"VECTORBT",
    "strategyId":STRATEGY_ID,
    "status":"CANDIDATE_FOUND" if selected_live else ("PAPER_CANDIDATE_FOUND" if selected_paper else "NO_DISCOVERY_PASS"),
    "pass":bool(selected_live),
    "authorization":"RESEARCH_ONLY",
    "liveTrading":False,
    "universeMode":"ALL_BINANCE_USDT_SPOT_INCLUDING_NEW_LISTINGS",
    "universeCount":len(universe),
    "symbolsScreenedCount":len(screen_data),
    "symbolsScreened":sorted(screen_data.keys()),
    "newListingsScreened":[x["symbol"] for x in universe if x.get("isNewListing") and x["symbol"] in screen_data],
    "deepValidatedSymbols":deep_symbols,
    "screenDays":SCREEN_DAYS,
    "deepDays":DEEP_DAYS,
    "fees":{"basePerSide":BASE_FEE,"stressPerSide":STRESS_FEE},
    "candidateFamilies":sorted({x["family"] for x in grid}),
    "selected":selected_live,
    "selectedPaperCandidate":selected_paper,
    "newListingWatch":new_watch,
    "finalists":finalists[:20],
    "dataFailures":failures,
    "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
}
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
