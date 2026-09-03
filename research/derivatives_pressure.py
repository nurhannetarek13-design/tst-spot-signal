#!/usr/bin/env python3
"""
Forward-only derivatives-to-spot pressure monitor.
Uses public Binance website/Futures market-data endpoints as a Spot context filter.
Research/paper only; never places futures orders and never uses leverage.
"""
import datetime as dt, json, pathlib, time, urllib.parse, urllib.request

SPOT="https://data-api.binance.vision"
WWW="https://www.binance.com"
OUT=pathlib.Path("validation/edges/derivatives-pressure-latest.json")
STRATEGY_ID="TST_DERIVATIVES_PRESSURE_V2"
MAX_PRICE=3.0
MIN_QV=20_000_000
MAX_QV=150_000_000
MAX_SYMBOLS=30
MAJORS={"BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","LTC","BCH","LINK","AVAX","DOT"}
EXCLUDED={"USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","AEUR","TRY","BRL","GBP","AUD","USD1","RLUSD","USDE","PAXG","XAUT"}

def get(base,path):
    req=urllib.request.Request(base+path,headers={
        "User-Agent":"Mozilla/5.0 tst-derivatives-pressure/2.0",
        "Accept":"application/json,text/plain,*/*",
        "Referer":"https://www.binance.com/",
    })
    with urllib.request.urlopen(req,timeout=20) as r:
        return json.load(r)

spot_info=get(SPOT,"/api/v3/exchangeInfo")
spot_tickers={x["symbol"]:x for x in get(SPOT,"/api/v3/ticker/24hr")}

universe=[]
for s in spot_info.get("symbols",[]):
    sym=s.get("symbol",""); base=s.get("baseAsset","")
    if s.get("status")!="TRADING" or s.get("quoteAsset")!="USDT" or not s.get("isSpotTradingAllowed"):
        continue
    if base in MAJORS or base in EXCLUDED or base.endswith(("UP","DOWN","BULL","BEAR")):
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
        qs=urllib.parse.urlencode({"symbol":s,"period":"15m","limit":8})
        oi=get(WWW,"/futures/data/openInterestHist?"+qs)
        taker=get(WWW,"/futures/data/takerlongshortRatio?"+qs)
        if not isinstance(oi,list) or len(oi)<2 or not isinstance(taker,list) or len(taker)<2:
            failures[s]="NO_FUTURES_SERIES"; continue

        oi0=float(oi[0].get("sumOpenInterestValue") or oi[0].get("sumOpenInterest") or 0)
        oi1=float(oi[-1].get("sumOpenInterestValue") or oi[-1].get("sumOpenInterest") or 0)
        oi_chg=(oi1/oi0-1) if oi0>0 else 0.0

        ratios=[float(z.get("buySellRatio") or 1) for z in taker[-4:]]
        taker_ratio=sum(ratios)/len(ratios)

        funding=None; basis=None
        try:
            prem=get(WWW,"/fapi/v1/premiumIndex?"+urllib.parse.urlencode({"symbol":s}))
            funding=float(prem.get("lastFundingRate") or 0)
            mark=float(prem.get("markPrice") or 0); index=float(prem.get("indexPrice") or 0)
            basis=(mark/index-1) if index>0 else None
        except Exception:
            pass

        score=50
        score += 20 if oi_chg>=0.02 else (10 if oi_chg>0 else -10)
        score += 20 if taker_ratio>=1.15 else (10 if taker_ratio>=1.05 else -10)
        if funding is not None:
            score += 5 if -0.0005<=funding<=0.0008 else (-5 if funding>0.0015 else 0)
        if basis is not None:
            score += 5 if abs(basis)<=0.003 else -5
        score=max(0,min(100,score))

        rows.append({
            **x,"score":score,"oiChange2h":oi_chg,"takerBuySellRatio1h":taker_ratio,
            "fundingRate":funding,"basis":basis,
            "paperLongPressure":bool(score>=75 and oi_chg>0 and taker_ratio>=1.05)
        })
        time.sleep(0.03)
    except Exception as e:
        failures[s]=str(e)

rows.sort(key=lambda x:x["score"],reverse=True)
status="FORWARD_ONLY" if rows else "NO_ACCESSIBLE_FUTURES_DATA"
report={
    "engine":"DERIVATIVES_PRESSURE","strategyId":STRATEGY_ID,"status":status,
    "pass":False,"authorization":"FORWARD_PAPER_CONTEXT_ONLY","liveTrading":False,
    "note":"Public derivatives context for Spot only. No futures execution, no leverage. Funding/basis are optional fallbacks; OI+taker are primary.",
    "candidates":rows[:15],"universeCount":len(universe),"usableCount":len(rows),"failures":failures,
    "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat()
}
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
