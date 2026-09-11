#!/usr/bin/env python3
import datetime as dt, json, pathlib, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np

SYMBOLS=["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","LINKUSDT","AVAXUSDT","DOTUSDT","LTCUSDT","TRXUSDT"]
INTERVAL="15m"; BAR_MS=15*60*1000; CHUNK=1000
START="2024-01-01T00:00:00+00:00"; END="2026-09-01T00:00:00+00:00"
LOOKBACK_24H=96; REBALANCE_BARS=4; TOP_N=3
HORIZONS={"1h":4,"2h":8,"4h":16,"8h":32}
OUT=pathlib.Path("validation/research/top-gainers-rs-raw-edge.json")

def ms(s): return int(dt.datetime.fromisoformat(s).timestamp()*1000)
def fetch_chunk(symbol,start,end):
    q=urllib.parse.urlencode({"symbol":symbol,"interval":INTERVAL,"limit":CHUNK,"startTime":start,"endTime":end})
    req=urllib.request.Request("https://data-api.binance.vision/api/v3/klines?"+q,headers={"User-Agent":"tst-top-gainers/1.0"})
    with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
def fetch_all():
    s0,e0=ms(START),ms(END); tasks=[]
    for sym in SYMBOLS:
        cur=s0
        while cur<e0:
            ce=min(e0-1,cur+CHUNK*BAR_MS-1);tasks.append((sym,cur,ce));cur+=CHUNK*BAR_MS
    data={s:[] for s in SYMBOLS}
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs={ex.submit(fetch_chunk,*t):t for t in tasks}
        for f in as_completed(futs):
            sym,_,_=futs[f]; data[sym].extend(f.result())
    out={}
    for s,rows in data.items():
        d={int(r[0]):float(r[4]) for r in rows}; out[s]=d
        print(s,"bars",len(d),flush=True)
    return out

def summarize(v):
    if not v:return {"n":0,"mean":None,"median":None,"hitRate":None}
    a=np.asarray(v,float);return {"n":int(a.size),"mean":float(a.mean()),"median":float(np.median(a)),"hitRate":float((a>0).mean())}

data=fetch_all(); common=sorted(set.intersection(*[set(data[s]) for s in SYMBOLS]))
C={s:np.array([data[s][t] for t in common],float) for s in SYMBOLS}; ts=np.array(common,dtype=np.int64)
max_h=max(HORIZONS.values()); events=[]
for i in range(LOOKBACK_24H,len(ts)-max_h,REBALANCE_BARS):
    scores=[]
    for s in SYMBOLS:
        p0=C[s][i-LOOKBACK_24H]; p=C[s][i]
        if p0>0:scores.append((p/p0-1,s))
    scores.sort(reverse=True); tops=[s for _,s in scores[:TOP_N]]; universe=[s for _,s in scores]
    for rank,s in enumerate(tops,1):
        fwd={h:float(C[s][i+n]/C[s][i]-1) for h,n in HORIZONS.items()}
        bench={h:float(np.mean([C[u][i+n]/C[u][i]-1 for u in universe])) for h,n in HORIZONS.items()}
        events.append({"ts":int(ts[i]),"symbol":s,"rank":rank,"ret24h":float(dict((sym,r) for r,sym in scores)[s] if False else next(r for r,sym in scores if sym==s)),"fwd":fwd,"bench":bench})
split_ts=int(ts[int(len(ts)*.70)])
res={}
for bucket,pred in [("IS",lambda e:e["ts"]<split_ts),("OOS",lambda e:e["ts"]>=split_ts)]:
    ev=[e for e in events if pred(e)];res[bucket]={}
    for h in HORIZONS:
        raw=[e["fwd"][h] for e in ev]; excess=[e["fwd"][h]-e["bench"][h] for e in ev]
        res[bucket][h]={"raw":summarize(raw),"excessVsUniverse":summarize(excess)}
# symbol-level OOS stability
sym_oos={}
for s in SYMBOLS:
    ev=[e for e in events if e["ts"]>=split_ts and e["symbol"]==s];sym_oos[s]={h:summarize([e["fwd"][h] for e in ev]) for h in HORIZONS}
qual_h=0
for h in HORIZONS:
    r=res["OOS"][h]["raw"];x=res["OOS"][h]["excessVsUniverse"]
    if r["n"]>=500 and r["mean"]>0 and r["median"]>0 and r["hitRate"]>=0.51 and x["mean"]>0 and x["median"]>0:qual_h+=1
pass_raw=qual_h>=2
report={"engine":"TOP_GAINERS_RS_RAW_EDGE_V1","status":"PASS_RAW_EDGE" if pass_raw else "REJECT_RAW_EDGE","scope":"CROSS_SECTIONAL_24H_TOP_GAINERS_PRICE_ONLY","symbols":SYMBOLS,"timeframe":INTERVAL,"window":{"start":START,"end":END,"split":"70/30 chronological"},"definition":{"rankWindow":"24h","rebalance":"1h","topN":TOP_N},"eventCount":len(events),"results":res,"symbolOOS":sym_oos,"qualifyingHorizons":qual_h,"passRawEdge":pass_raw,"nextStep":"ADD_LIQUIDITY_BTC_REGIME_AND_L2" if pass_raw else "DO_NOT_TUNE_EXECUTION","authorization":"RESEARCH_ONLY","liveTrading":False,"generatedAt":dt.datetime.now(dt.timezone.utc).isoformat()}
OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
