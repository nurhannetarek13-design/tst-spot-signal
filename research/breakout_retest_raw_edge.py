#!/usr/bin/env python3
import datetime as dt, json, pathlib, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np

SYMBOLS=["BTCUSDT","ETHUSDT","SOLUSDT"]
INTERVAL="15m"
START="2024-01-01T00:00:00+00:00"
END="2026-09-01T00:00:00+00:00"
LOOKBACK=20
ATR_LEN=14
RETEST_MAX_BARS=8
RETEST_TOL_ATR=0.25
CONT_BUF_ATR=0.10
HORIZONS={"1h":4,"2h":8,"4h":16,"8h":32}
OUT=pathlib.Path("validation/research/breakout-retest-raw-edge.json")
BAR_MS=15*60*1000
CHUNK_BARS=1000

def ms(s): return int(dt.datetime.fromisoformat(s).timestamp()*1000)

def fetch_chunk(symbol,start_ms,end_ms):
    q=urllib.parse.urlencode({"symbol":symbol,"interval":INTERVAL,"limit":CHUNK_BARS,"startTime":start_ms,"endTime":end_ms})
    req=urllib.request.Request("https://data-api.binance.vision/api/v3/klines?"+q,headers={"User-Agent":"tst-breakout-retest/1.2"})
    with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)

def fetch_all():
    start,end=ms(START),ms(END)
    tasks=[]
    for symbol in SYMBOLS:
        cur=start
        while cur<end:
            chunk_end=min(end-1,cur+CHUNK_BARS*BAR_MS-1)
            tasks.append((symbol,cur,chunk_end));cur+=CHUNK_BARS*BAR_MS
    data={s:[] for s in SYMBOLS}
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs={ex.submit(fetch_chunk,*t):t for t in tasks}
        for f in as_completed(futs):
            symbol,_,_=futs[f];data[symbol].extend(f.result())
    for s in SYMBOLS:
        dedup={int(r[0]):r for r in data[s]}
        data[s]=[dedup[k] for k in sorted(dedup)]
        print(s,"bars",len(data[s]),flush=True)
    return data

def atr(h,l,c,n=14):
    tr=np.full(len(c),np.nan)
    for i in range(1,len(c)): tr[i]=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
    out=np.full(len(c),np.nan)
    kernel=np.ones(n)/n
    vals=np.convolve(np.nan_to_num(tr,nan=0.0),kernel,mode='valid')
    out[n-1:n-1+len(vals)]=vals
    return out

def summarize(vals):
    if not vals:return {"n":0,"mean":None,"median":None,"hitRate":None}
    a=np.asarray(vals,float)
    return {"n":int(len(a)),"mean":float(np.mean(a)),"median":float(np.median(a)),"hitRate":float(np.mean(a>0))}

def analyze(symbol,rows):
    ts=np.array([int(x[0]) for x in rows],dtype=np.int64)
    h=np.array([float(x[2]) for x in rows]);l=np.array([float(x[3]) for x in rows]);c=np.array([float(x[4]) for x in rows])
    a=atr(h,l,c,ATR_LEN);events=[];last_event=-9999;max_h=max(HORIZONS.values())
    for i in range(max(LOOKBACK,ATR_LEN)+1,len(c)-RETEST_MAX_BARS-max_h-1):
        if i-last_event<RETEST_MAX_BARS: continue
        level=float(np.max(h[i-LOOKBACK:i]))
        if not (c[i]>level and np.isfinite(a[i])): continue
        chosen=None
        for j in range(i+1,i+RETEST_MAX_BARS+1):
            if not np.isfinite(a[j]): continue
            touched=(l[j] <= level + RETEST_TOL_ATR*a[j]) and (h[j] >= level - RETEST_TOL_ATR*a[j])
            held=c[j] >= level - RETEST_TOL_ATR*a[j]
            continuation=c[j] > level + CONT_BUF_ATR*a[j]
            if touched and held and continuation: chosen=j; break
        if chosen is None: continue
        entry=float(c[chosen]);fwd={name:float(c[chosen+bars]/entry-1) for name,bars in HORIZONS.items()}
        future_hi=float(np.max(h[chosen+1:chosen+max_h+1]));future_lo=float(np.min(l[chosen+1:chosen+max_h+1]))
        events.append({"signalTs":int(ts[chosen]),"level":level,"entry":entry,"fwd":fwd,"mfe8h":future_hi/entry-1,"mae8h":future_lo/entry-1});last_event=chosen
    split=int(len(c)*0.70);split_ts=int(ts[split]);out={"events":len(events),"splitTs":split_ts,"IS":{},"OOS":{}}
    for bucket,flt in [("IS",lambda e:e["signalTs"]<split_ts),("OOS",lambda e:e["signalTs"]>=split_ts)]:
        ev=[e for e in events if flt(e)]
        for name in HORIZONS: out[bucket][name]=summarize([e["fwd"][name] for e in ev])
        out[bucket]["mfe8hMedian"]=float(np.median([e["mfe8h"] for e in ev])) if ev else None
        out[bucket]["mae8hMedian"]=float(np.median([e["mae8h"] for e in ev])) if ev else None
    print(symbol,"events",len(events),"OOS",out["OOS"]["1h"]["n"],flush=True)
    return out

raw=fetch_all();results={s:analyze(s,raw[s]) for s in SYMBOLS}
qual_symbols=0
for s,r in results.items():
    good=sum(1 for hn in HORIZONS if r["OOS"][hn]["n"]>=30 and (r["OOS"][hn]["median"] or 0)>0 and (r["OOS"][hn]["hitRate"] or 0)>=0.52)
    if good>=2: qual_symbols+=1
agg={}
for hn in HORIZONS:
    num=den=0.0
    for r in results.values():
        m=r["OOS"][hn]
        if m["n"] and m["mean"] is not None:num+=m["mean"]*m["n"];den+=m["n"]
    agg[hn]={"weightedMean":num/den if den else None}
pass_raw=qual_symbols>=2 and all((agg[h]["weightedMean"] or -1)>0 for h in HORIZONS)
report={"engine":"BREAKOUT_RETEST_RAW_EDGE_V1","status":"PASS_RAW_EDGE" if pass_raw else "REJECT_RAW_EDGE","scope":"PRICE_SETUP_ONLY_NO_L2","symbols":SYMBOLS,"timeframe":INTERVAL,"window":{"start":START,"end":END,"split":"70/30 chronological"},"definition":{"lookback":LOOKBACK,"retestMaxBars":RETEST_MAX_BARS,"retestToleranceATR":RETEST_TOL_ATR,"continuationBufferATR":CONT_BUF_ATR},"results":results,"aggregate":agg,"qualifyingSymbols":qual_symbols,"passRawEdge":pass_raw,"nextStep":"FORWARD_L2_CONFIRMATION" if pass_raw else "DO_NOT_TUNE_L2_OR_EXITS","authorization":"RESEARCH_ONLY","liveTrading":False,"generatedAt":dt.datetime.now(dt.timezone.utc).isoformat()}
OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
