#!/usr/bin/env python3
"""
Forward-only derivatives-to-spot pressure monitor.
Uses public Binance USD-M data as a predictive filter for Spot candidates.
Research/paper only; it never trades futures.
"""
import datetime as dt, json, pathlib, time, urllib.parse, urllib.request
from urllib.error import HTTPError

SPOT="https://data-api.binance.vision"
FAPI="https://fapi.binance.com"
OUT=pathlib.Path("validation/edges/derivatives-pressure-latest.json")
STRATEGY_ID="TST_DERIVATIVES_PRESSURE_V1"
MAX_PRICE=3.0
MIN_QV=20_000_000
MAX_QV=150_000_000
MAX_SYMBOLS=30
MAJORS={"BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","LTC","BCH","LINK","AVAX","DOT"}

def get(base,path):
    req=urllib.request.Request(base+path,headers={"User-Agent":"tst-derivatives-pressure/1.0"})
    with urllib.request.urlopen(req,timeout=20) as r:return json.load(r)

spot_info=get(SPOT,"/api/v3/exchangeInfo")
spot_tickers={x["symbol"]:x for x in get(SPOT,"/api/v3/ticker/24hr")}
try:
    fut_info=get(FAPI,"/fapi/v1/exchangeInfo")
except HTTPError as e:
    if e.code == 451:
        report={
            "engine":"DERIVATIVES_PRESSURE","strategyId":STRATEGY_ID,"status":"RUNTIME_BLOCKED_451",
            "pass":False,"authorization":"FORWARD_PAPER_ONLY","liveTrading":False,
            "note":"GitHub-hosted runner is blocked from Binance USD-M Futures. Active derivatives monitoring has moved to the Cloudflare Worker.",
            "candidates":[],"universeCount":0,"failures":{"runtime":"HTTP 451"},
            "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat()
        }
        OUT.parent.mkdir(parents=True,exist_ok=True)
        OUT.write_text(json.dumps(report,indent=2))
        print(json.dumps(report,indent=2))
        raise SystemExit(0)
    raise
fut_symbols={x["symbol"] for x in fut_info.get("symbols",[]) if x.get("status")=="TRADING" and x.get("contractType")=="PERPETUAL"}

universe=[]
for s in spot_info.get("symbols",[]):
    sym=s.get("symbol",""); base=s.get("baseAsset","")
    if s.get("status")!="TRADING" or s.get("quoteAsset")!="USDT" or not s.get("isSpotTradingAllowed") or base in MAJORS or sym not in fut_symbols:
        continue
    t=spot_tickers.get(sym,{})
    px=float(t.get("lastPrice") or 0); qv=float(t.get("quoteVolume") or 0)
    if 0<px<=MAX_PRICE and MIN_QV<=qv<=MAX_QV:
        universe.append({"symbol":sym,"price":px,"quoteVolume24h":qv})
universe.sort(key=lambda x:x["quoteVolume24h"],reverse=True)
universe=universe[:MAX_SYMBOLS]

rows=[]; failures={}
for x in universe:
    s=x["symbol"]
    try:
        prem=get(FAPI,"/fapi/v1/premiumIndex?"+urllib.parse.urlencode({"symbol":s}))
        oi=get(FAPI,"/futures/data/openInterestHist?"+urllib.parse.urlencode({"symbol":s,"period":"15m","limit":8}))
        taker=get(FAPI,"/futures/data/takerlongshortRatio?"+urllib.parse.urlencode({"symbol":s,"period":"15m","limit":8}))
        if len(oi)<2 or len(taker)<2: continue
        oi0=float(oi[0].get("sumOpenInterestValue") or oi[0].get("sumOpenInterest") or 0)
        oi1=float(oi[-1].get("sumOpenInterestValue") or oi[-1].get("sumOpenInterest") or 0)
        oi_chg=(oi1/oi0-1) if oi0>0 else 0
        ratios=[float(z.get("buySellRatio") or 1) for z in taker[-4:]]
        taker_ratio=sum(ratios)/len(ratios)
        funding=float(prem.get("lastFundingRate") or 0)
        mark=float(prem.get("markPrice") or 0); index=float(prem.get("indexPrice") or 0)
        basis=(mark/index-1) if index>0 else 0
        score=50
        score += 18 if oi_chg>=0.02 else (8 if oi_chg>0 else -8)
        score += 18 if taker_ratio>=1.15 else (8 if taker_ratio>=1.05 else -8)
        score += 8 if -0.0005<=funding<=0.0008 else (-8 if funding>0.0015 else 0)
        score += 6 if abs(basis)<=0.003 else -6
        score=max(0,min(100,score))
        rows.append({
            **x,"score":score,"oiChange2h":oi_chg,"takerBuySellRatio1h":taker_ratio,
            "fundingRate":funding,"basis":basis,
            "paperLongPressure":bool(score>=78 and oi_chg>0 and taker_ratio>=1.05)
        })
        time.sleep(0.04)
    except Exception as e: failures[s]=str(e)

rows.sort(key=lambda x:x["score"],reverse=True)
report={
    "engine":"DERIVATIVES_PRESSURE","strategyId":STRATEGY_ID,"status":"FORWARD_ONLY",
    "pass":False,"authorization":"FORWARD_PAPER_ONLY","liveTrading":False,
    "note":"Futures data is used only as a Spot signal/filter. No futures execution or leverage.",
    "candidates":rows[:15],"universeCount":len(universe),"failures":failures,
    "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat()
}
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
