#!/usr/bin/env python3
"""30-day portfolio-selection validation for frozen derivatives-pressure 48h strategy.

Same frozen signal and same 3-position execution policy as the 14-day test. Only the
observation window is extended to 30 days to determine whether the prior failure was
purely a sample-size artifact. Research only; no order placement.
"""
import datetime as dt, json, pathlib, statistics, time, urllib.parse, urllib.request
from collections import defaultdict

SPOT='https://data-api.binance.vision'; WWW='https://www.binance.com'
OUT=pathlib.Path('validation/edges/derivatives-pressure-48h-portfolio-selection-30d.json')
PERIOD='15m'; BAR_MS=15*60*1000; LOOKBACK_DAYS=30; H48=192
SYMBOLS=['PUMPUSDT','ASTERUSDT','WLDUSDT','ENAUSDT','NEARUSDT','SUIUSDT','ARBUSDT','MARSCOINUSDT','ZKCUSDT','SAHARAUSDT']
COSTS={'base_20bps':0.0020,'stress_40bps':0.0040,'stress_60bps':0.0060}
MAX_CONCURRENT=3

def get(base,path):
    req=urllib.request.Request(base+path,headers={'User-Agent':'Mozilla/5.0 tst-portfolio-selection-30d/1.0','Accept':'application/json,text/plain,*/*','Referer':'https://www.binance.com/'})
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
def metrics(trades,key):
    vals=[t[key] for t in trades]
    if not vals: return {'n':0,'meanPct':None,'medianPct':None,'hitRatePct':None,'profitFactor':None,'expectancyPct':None}
    wins=sum(v for v in vals if v>0); losses=-sum(v for v in vals if v<0)
    pf=None if losses<=0 else wins/losses
    return {'n':len(vals),'meanPct':round(100*sum(vals)/len(vals),4),'medianPct':round(100*statistics.median(vals),4),'hitRatePct':round(100*sum(v>0 for v in vals)/len(vals),2),'profitFactor':None if pf is None else round(pf,4),'expectancyPct':round(100*sum(vals)/len(vals),4)}
def gate(m):
    return m['n']>=10 and m['meanPct'] is not None and m['meanPct']>=1.0 and m['medianPct']>0 and m['hitRatePct']>55 and m['profitFactor'] is not None and m['profitFactor']>1.2

now=dt.datetime.now(dt.timezone.utc); end_ms=int(now.timestamp()*1000); end_ms-=end_ms%BAR_MS; start_ms=end_ms-LOOKBACK_DAYS*86400000
raw=[]; failures={}
for s in SYMBOLS:
    try:
        oi=fetch_series('/futures/data/openInterestHist',s,start_ms,end_ms)
        tk=fetch_series('/futures/data/takerlongshortRatio',s,start_ms,end_ms)
        sp=fetch_spot(s,start_ms,end_ms+(48*60+15)*60000)
        om={int(r['timestamp']):r for r in oi}; tm={int(r['timestamp']):r for r in tk}; sm={int(r[0]):r for r in sp}
        prev=False
        for ts in sorted(set(om)&set(tm)&set(sm)):
            p=ts-8*BAR_MS
            if p not in om: prev=False; continue
            oi0=float(om[p].get('sumOpenInterestValue') or om[p].get('sumOpenInterest') or 0); oi1=float(om[ts].get('sumOpenInterestValue') or om[ts].get('sumOpenInterest') or 0)
            if oi0<=0: prev=False; continue
            if any(ts-j*BAR_MS not in tm for j in range(4)): prev=False; continue
            oichg=oi1/oi0-1; tr=sum(float(tm[ts-j*BAR_MS].get('buySellRatio') or 1) for j in range(4))/4
            score=50+(20 if oichg>=0.02 else 10 if oichg>0 else -10)+(20 if tr>=1.15 else 10 if tr>=1.05 else -10)
            cond=score>=80 and oichg>0 and tr>=1.05; onset=cond and not prev; prev=cond
            if not onset: continue
            entry_ts=ts+BAR_MS; exit_ts=entry_ts+H48*BAR_MS
            if entry_ts not in sm or exit_ts not in sm: continue
            entry=float(sm[entry_ts][1]); exit_px=float(sm[exit_ts][4]); gross=exit_px/entry-1
            t={'symbol':s,'entryTs':entry_ts,'exitTs':exit_ts,'gross':gross,'score':score,'oiChange2h':oichg,'takerBuySellRatio1h':tr}
            for n,c in COSTS.items(): t[n]=gross-c
            raw.append(t)
    except Exception as e: failures[s]=str(e)

batches=defaultdict(list)
for t in raw: batches[t['entryTs']].append(t)
accepted=[]; rejected={'sameSymbolOverlap':0,'portfolioCapacity':0}; open_positions=[]
for ts in sorted(batches):
    open_positions=[p for p in open_positions if p['exitTs']>ts]
    slots=max(0,MAX_CONCURRENT-len(open_positions))
    candidates=[]
    for t in batches[ts]:
        if any(p['symbol']==t['symbol'] for p in open_positions): rejected['sameSymbolOverlap']+=1
        else: candidates.append(t)
    candidates.sort(key=lambda x:(-x['score'],-x['oiChange2h'],-x['takerBuySellRatio1h'],x['symbol']))
    for i,t in enumerate(candidates):
        if i<slots:
            accepted.append(t); open_positions.append(t)
        else: rejected['portfolioCapacity']+=1

accepted.sort(key=lambda x:x['entryTs']); cut=int(len(accepted)*0.7); oos=accepted[cut:]
scenarios={k:{'all':metrics(accepted,k),'oos30':metrics(oos,k)} for k in COSTS}
events=sorted([(t['exitTs'],t['base_20bps']/MAX_CONCURRENT) for t in accepted])
equity=1.0; peak=1.0; maxdd=0.0
for _,r in events:
    equity*=1+r; peak=max(peak,equity); maxdd=max(maxdd,1-equity/peak)
validation={'passed':gate(scenarios['base_20bps']['all']) and gate(scenarios['base_20bps']['oos30']) and gate(scenarios['stress_60bps']['all']),
            'baseAllPassed':gate(scenarios['base_20bps']['all']),'baseOosPassed':gate(scenarios['base_20bps']['oos30']),'stress60Passed':gate(scenarios['stress_60bps']['all'])}
report={'schemaVersion':1,'strategyId':'TST_DERIVATIVES_PRESSURE_V2_48H_EXECUTABLE_V1','validationType':'PORTFOLIO_SELECTION_RANKED_BATCH_30D','authorization':'RESEARCH_ONLY','liveTrading':False,'selectionPolicy':'same-entry-bar candidates ranked by frozen pressure score, then OI change, then taker ratio','constraints':{'lookbackDays':LOOKBACK_DAYS,'maxConcurrentPositions':MAX_CONCURRENT,'sameSymbolOverlapAllowed':False,'holdingPeriodHours':48},'rawSignals':len(raw),'acceptedTrades':len(accepted),'rejected':rejected,'scenarios':scenarios,'portfolio':{'endingEquityMultiple':round(equity,4),'maxDrawdownPct':round(maxdd*100,2)},'validation':validation,'failures':failures,'generatedAt':now.isoformat()}
OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(report,indent=2)); print(json.dumps(report,indent=2))
