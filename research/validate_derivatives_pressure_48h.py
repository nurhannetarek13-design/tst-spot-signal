#!/usr/bin/env python3
import datetime as dt, json, math, pathlib, statistics, time, urllib.parse, urllib.request
SPOT='https://data-api.binance.vision'; WWW='https://www.binance.com'
OUT=pathlib.Path('validation/edges/derivatives-pressure-48h-validation.json')
PERIOD='15m'; BAR_MS=15*60*1000; LOOKBACK_DAYS=14; H48=192
SYMBOLS=['PUMPUSDT','ASTERUSDT','WLDUSDT','ENAUSDT','NEARUSDT','SUIUSDT','ARBUSDT','MARSCOINUSDT','ZKCUSDT','SAHARAUSDT']

def get(base,path):
    req=urllib.request.Request(base+path,headers={'User-Agent':'Mozilla/5.0 tst-derivatives-pressure-48h/1.0','Accept':'application/json,text/plain,*/*','Referer':'https://www.binance.com/'})
    with urllib.request.urlopen(req,timeout=30) as r: return json.load(r)
def qs(p): return urllib.parse.urlencode(p)
def fetch_series(path,symbol,start_ms,end_ms,limit=500):
    rows=[]; cursor=start_ms
    while cursor<end_ms:
        b=get(WWW,path+'?'+qs({'symbol':symbol,'period':PERIOD,'limit':limit,'startTime':cursor,'endTime':end_ms}))
        if not isinstance(b,list) or not b: break
        rows.extend(b); last=int(b[-1].get('timestamp') or 0)
        if last<=cursor: break
        cursor=last+BAR_MS
        if len(b)<limit: break
        time.sleep(0.03)
    d={int(r.get('timestamp') or 0):r for r in rows if int(r.get('timestamp') or 0)>0}
    return [d[k] for k in sorted(d)]
def fetch_spot(symbol,start_ms,end_ms):
    rows=[]; cursor=start_ms
    while cursor<end_ms:
        b=get(SPOT,'/api/v3/klines?'+qs({'symbol':symbol,'interval':PERIOD,'limit':1000,'startTime':cursor,'endTime':end_ms}))
        if not isinstance(b,list) or not b: break
        rows.extend(b); last=int(b[-1][0])
        if last<=cursor: break
        cursor=last+BAR_MS
        if len(b)<1000: break
        time.sleep(0.03)
    d={int(r[0]):r for r in rows}; return [d[k] for k in sorted(d)]
def med(xs): return statistics.median(xs) if xs else None
def metrics(events):
    if not events: return {'n':0,'meanPct':None,'medianPct':None,'hitRatePct':None,'medianMfeMaeRatio':None}
    rs=[e['ret'] for e in events]; mf=[e['mfe'] for e in events]; ma=[e['mae'] for e in events]
    mmae=med(ma); ratio=None if not mmae or mmae<=0 else med(mf)/mmae
    return {'n':len(events),'meanPct':round(100*sum(rs)/len(rs),4),'medianPct':round(100*med(rs),4),'hitRatePct':round(100*sum(x>0 for x in rs)/len(rs),2),'medianMfePct':round(100*med(mf),4),'medianMaePct':round(100*mmae,4),'medianMfeMaeRatio':None if ratio is None else round(ratio,4)}
def pass_gate(m): return m['n']>=10 and m['meanPct']>=1.5 and m['medianPct']>0 and m['hitRatePct']>55 and m['medianMfeMaeRatio'] is not None and m['medianMfeMaeRatio']>=2
now=dt.datetime.now(dt.timezone.utc); end_ms=int(now.timestamp()*1000); end_ms-=end_ms%BAR_MS; start_ms=end_ms-LOOKBACK_DAYS*86400000
events=[]; per_symbol={}; failures={}
for s in SYMBOLS:
    try:
        oi=fetch_series('/futures/data/openInterestHist',s,start_ms,end_ms); tk=fetch_series('/futures/data/takerlongshortRatio',s,start_ms,end_ms); sp=fetch_spot(s,start_ms,end_ms+48*3600000)
        om={int(r['timestamp']):r for r in oi}; tm={int(r['timestamp']):r for r in tk}; sm={int(r[0]):r for r in sp}; tslist=sorted(set(om)&set(tm)&set(sm)); prev=False; sev=[]
        for ts in tslist:
            p=ts-8*BAR_MS
            if p not in om: prev=False; continue
            a=float(om[p].get('sumOpenInterestValue') or om[p].get('sumOpenInterest') or 0); b=float(om[ts].get('sumOpenInterestValue') or om[ts].get('sumOpenInterest') or 0)
            if a<=0: prev=False; continue
            oichg=b/a-1; rr=[float(tm.get(ts-j*BAR_MS,{}).get('buySellRatio') or 1) for j in range(4)]
            if any(ts-j*BAR_MS not in tm for j in range(4)): prev=False; continue
            tr=sum(rr)/4; score=50+(20 if oichg>=0.02 else 10 if oichg>0 else -10)+(20 if tr>=1.15 else 10 if tr>=1.05 else -10)
            cond=score>=80 and oichg>0 and tr>=1.05; onset=cond and not prev; prev=cond
            if not onset: continue
            fts=ts+H48*BAR_MS
            if fts not in sm: continue
            entry=float(sm[ts][4]); fut=[sm.get(ts+k*BAR_MS) for k in range(1,H48+1)]; fut=[x for x in fut if x]
            if not fut: continue
            ev={'symbol':s,'timestamp':ts,'ret':float(sm[fts][4])/entry-1,'mfe':max(float(x[2]) for x in fut)/entry-1,'mae':max(0.0,1-min(float(x[3]) for x in fut)/entry)}
            sev.append(ev); events.append(ev)
        per_symbol[s]=metrics(sev)
    except Exception as e: failures[s]=str(e)
events.sort(key=lambda e:e['timestamp']); n=len(events); cut=int(n*0.7); chrono_in=events[:cut]; chrono_oos=events[cut:]
folds=[]
for i in range(4):
    a=int(n*i/4); b=int(n*(i+1)/4); m=metrics(events[a:b]); folds.append({'fold':i+1,'metrics':m,'passed':pass_gate(m)})
allm=metrics(events); inm=metrics(chrono_in); oosm=metrics(chrono_oos)
report={'schemaVersion':1,'strategyId':'TST_DERIVATIVES_PRESSURE_V2_48H_FROZEN','authorization':'RESEARCH_ONLY','liveTrading':False,'frozenEventDefinition':'coreScore>=80 AND oiChange2h>0 AND takerBuySellRatio1h>=1.05; false->true onset only; horizon=48h','lookbackDays':LOOKBACK_DAYS,'all':allm,'chronologicalInSample70':inm,'chronologicalOOS30':oosm,'walkForwardQuarters':folds,'perSymbol':per_symbol,'validation':{'passed':pass_gate(allm) and pass_gate(oosm) and sum(1 for f in folds if f['passed'])>=3,'allPassed':pass_gate(allm),'oosPassed':pass_gate(oosm),'positiveFolds':sum(1 for f in folds if f['passed'])},'failures':failures,'generatedAt':now.isoformat()}
OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(report,indent=2)); print(json.dumps(report,indent=2))
