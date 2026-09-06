#!/usr/bin/env python3
"""Research-only Tardis microstructure diagnostic for Binance USD-M.

Uses free first-day-of-month Tardis samples. This is discovery only: the sample
calendar is sparse and must never be treated as an unbiased production backtest.
"""
from __future__ import annotations

import csv, datetime as dt, gzip, io, json, math, pathlib, statistics, urllib.request
from collections import defaultdict

from tardis_sample_loader import sample_url

OUT = pathlib.Path("validation/microstructure/tardis-discovery.json")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
MONTHS = [(2024,1), (2024,2), (2024,3), (2024,4), (2024,5), (2024,6)]
UA = "tst-microstructure-discovery/1.0"


def f(x, default=0.0):
    try: return float(x)
    except Exception: return default


def minute_us(ts): return (int(ts) // 60_000_000) * 60_000_000


def stream_rows(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=120) as r:
        with gzip.GzipFile(fileobj=r) as gz:
            text = io.TextIOWrapper(gz, encoding="utf-8-sig", errors="replace", newline="")
            yield from csv.DictReader(text)


def aggregate_liquidations(year, month, symbols):
    out = {s: defaultdict(lambda: {"liqCount":0,"liqNotional":0.0,"buyLiq":0.0,"sellLiq":0.0}) for s in symbols}
    url = sample_url("liquidations", year, month, "PERPETUALS")
    for r in stream_rows(url):
        s = (r.get("symbol") or "").upper()
        if s not in out: continue
        ts = r.get("timestamp") or r.get("local_timestamp")
        if not ts: continue
        m = minute_us(ts); px=f(r.get("price")); qty=f(r.get("amount") or r.get("quantity")); n=px*qty
        side=(r.get("side") or "").lower(); a=out[s][m]; a["liqCount"]+=1; a["liqNotional"]+=n
        if side=="buy": a["buyLiq"]+=n
        elif side=="sell": a["sellLiq"]+=n
    return out


def aggregate_trades(year, month, symbol):
    out=defaultdict(lambda:{"tradeCount":0,"buyNotional":0.0,"sellNotional":0.0,"lastPrice":None})
    for r in stream_rows(sample_url("trades",year,month,symbol)):
        ts=r.get("timestamp") or r.get("local_timestamp")
        if not ts: continue
        m=minute_us(ts); px=f(r.get("price")); qty=f(r.get("amount") or r.get("quantity")); n=px*qty
        side=(r.get("side") or "").lower(); a=out[m]; a["tradeCount"]+=1; a["lastPrice"]=px
        if side=="buy": a["buyNotional"]+=n
        elif side=="sell": a["sellNotional"]+=n
    return out


def aggregate_book(year, month, symbol):
    out={}
    for r in stream_rows(sample_url("book_ticker",year,month,symbol)):
        ts=r.get("timestamp") or r.get("local_timestamp")
        if not ts: continue
        m=minute_us(ts); bid=f(r.get("bid_price") or r.get("best_bid_price")); ask=f(r.get("ask_price") or r.get("best_ask_price")); bq=f(r.get("bid_amount") or r.get("best_bid_amount")); aq=f(r.get("ask_amount") or r.get("best_ask_amount"))
        if bid<=0 or ask<=0: continue
        mid=(bid+ask)/2; out[m]={"spreadBps":10000*(ask-bid)/mid,"topImbalance":bq/(bq+aq) if bq+aq>0 else 0.5}
    return out


def med(xs): return statistics.median(xs) if xs else None

records=[]; failures={}; data_counts={}
for y,mo in MONTHS:
    try: liq_all=aggregate_liquidations(y,mo,set(SYMBOLS))
    except Exception as e:
        failures[f"liquidations-{y}-{mo:02d}"]=f"{type(e).__name__}: {e}"; continue
    for s in SYMBOLS:
        key=f"{s}-{y}-{mo:02d}"
        try:
            trades=aggregate_trades(y,mo,s); book=aggregate_book(y,mo,s); liqs=liq_all[s]
            mins=sorted(set(trades)&set(book))
            data_counts[key]={"minutes":len(mins),"liquidationMinutes":len(liqs)}
            prices={m:trades[m]["lastPrice"] for m in mins if trades[m]["lastPrice"]}
            for i,m in enumerate(mins):
                t=trades[m]; b=book[m]; l=liqs.get(m,{"liqCount":0,"liqNotional":0.0,"buyLiq":0.0,"sellLiq":0.0})
                total=t["buyNotional"]+t["sellNotional"]
                if total<=0: continue
                future=prices.get(m+60*60_000_000)
                px=t["lastPrice"]
                if not future or not px: continue
                records.append({"symbol":s,"month":f"{y}-{mo:02d}","minute":m,"aggrBuyShare":t["buyNotional"]/total,"spreadBps":b["spreadBps"],"topImbalance":b["topImbalance"],"liqNotional":l["liqNotional"],"liqSellShare":l["sellLiq"]/(l["buyLiq"]+l["sellLiq"]) if l["buyLiq"]+l["sellLiq"]>0 else None,"fwd1h":future/px-1})
        except Exception as e: failures[key]=f"{type(e).__name__}: {e}"

liq_values=[r["liqNotional"] for r in records if r["liqNotional"]>0]
liq_cut=med(liq_values) or math.inf
hi=[r for r in records if r["liqNotional"]>=liq_cut and r["liqNotional"]>0]
lo=[r for r in records if r["liqNotional"]==0]

def summary(rs):
    if not rs:return {"n":0}
    fw=[r["fwd1h"] for r in rs]; return {"n":len(rs),"meanFwd1hPct":round(100*sum(fw)/len(fw),5),"medianFwd1hPct":round(100*statistics.median(fw),5),"hitRatePct":round(100*sum(x>0 for x in fw)/len(fw),2),"medianAggBuyShare":round(statistics.median(r["aggrBuyShare"] for r in rs),4),"medianSpreadBps":round(statistics.median(r["spreadBps"] for r in rs),4),"medianTopImbalance":round(statistics.median(r["topImbalance"] for r in rs),4)}

# Directional diagnostic: sell-side liquidations + aggressive buying may mark absorption/continuation.
absorb=[r for r in hi if (r["liqSellShare"] or 0)>=0.6 and r["aggrBuyShare"]>=0.55]
crowded=[r for r in hi if (r["liqSellShare"] or 0)<0.4 and r["aggrBuyShare"]>=0.55]
report={"schemaVersion":1,"authorization":"RESEARCH_ONLY","liveTrading":False,"sampleDesign":"TARDIS_FREE_FIRST_DAY_OF_MONTH_ONLY","months":[f"{y}-{m:02d}" for y,m in MONTHS],"symbols":SYMBOLS,"recordCount":len(records),"liquidationMedianNotionalCut":None if math.isinf(liq_cut) else liq_cut,"groups":{"highLiquidation":summary(hi),"noLiquidation":summary(lo),"sellLiquidationPlusAggressiveBuy":summary(absorb),"buyLiquidationPlusAggressiveBuy":summary(crowded)},"dataCounts":data_counts,"failures":failures,"decisionRule":"Discovery only. Do not promote a candidate from this sparse calendar sample; require replication across withheld months and Binance Vision long-window features.","generatedAt":dt.datetime.now(dt.timezone.utc).isoformat()}
OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(report,indent=2)); print(json.dumps(report,indent=2))
