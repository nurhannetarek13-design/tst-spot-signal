#!/usr/bin/env python3
"""ATR exit-design validation for frozen derivatives-pressure signal.

Source concept adapted from user-supplied TradingView indicator:
- ATR(14)
- support lookback 20 bars
- SL 1.5 ATR
- TP1 3.0 ATR equivalent (rewardToRisk 2 * TP1 multiplier 1.5)
- TP2 4.0 ATR equivalent (rewardToRisk 2 * TP2 multiplier 2.0)

Two pre-declared variants are tested without tuning after results:
1) SUPPORT_ANCHORED: faithful to the supplied script; levels anchored to rolling support.
2) ENTRY_ANCHORED: same distances, but anchored to actual entry price for executable risk control.

Long-only, because the production target is Binance Spot. Partial exit: 50% at TP1, 50% at TP2.
If SL and TP are touched inside the same 15m candle, assume SL first (conservative intrabar rule).
Any remaining size is time-exited at 48h. Research only, no order placement.
"""
import datetime as dt, json, pathlib, statistics, time, urllib.parse, urllib.request

SPOT='https://data-api.binance.vision'; WWW='https://www.binance.com'
OUT=pathlib.Path('validation/edges/derivatives-pressure-atr-exits.json')
PERIOD='15m'; BAR_MS=15*60*1000; LOOKBACK_DAYS=30; MAX_HOLD=192
SYMBOLS=['PUMPUSDT','ASTERUSDT','WLDUSDT','ENAUSDT','NEARUSDT','SUIUSDT','ARBUSDT','MARSCOINUSDT','ZKCUSDT','SAHARAUSDT']
COST=0.0020
ATR_N=14; SUPPORT_N=20
SL_ATR=1.5; TP1_ATR=3.0; TP2_ATR=4.0


def get(base,path):
    req=urllib.request.Request(base+path,headers={'User-Agent':'Mozilla/5.0 tst-atr-exit/1.0','Accept':'application/json,text/plain,*/*','Referer':'https://www.binance.com/'})
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
def true_range(prev_close,h,l): return max(h-l,abs(h-prev_close),abs(l-prev_close))
def atr_at(rows, idx, n=ATR_N):
    if idx < n: return None
    trs=[]
    for i in range(idx-n+1,idx+1):
        prev=float(rows[i-1][4]); h=float(rows[i][2]); l=float(rows[i][3]); trs.append(true_range(prev,h,l))
    return sum(trs)/len(trs)
def support_at(rows,idx,n=SUPPORT_N):
    if idx < n-1: return None
    return min(float(rows[i][3]) for i in range(idx-n+1,idx+1))
def metrics(vals):
    if not vals: return {'n':0,'meanPct':None,'medianPct':None,'hitRatePct':None,'profitFactor':None}
    wins=sum(v for v in vals if v>0); losses=-sum(v for v in vals if v<0)
    pf=None if losses<=0 else wins/losses
    return {'n':len(vals),'meanPct':round(100*sum(vals)/len(vals),4),'medianPct':round(100*statistics.median(vals),4),'hitRatePct':round(100*sum(v>0 for v in vals)/len(vals),2),'profitFactor':None if pf is None else round(pf,4)}
def gate(m):
    return m['n']>=10 and m['meanPct'] is not None and m['meanPct']>=1 and m['medianPct']>0 and m['hitRatePct']>55 and m['profitFactor'] is not None and m['profitFactor']>1.2

def simulate(rows, idx_by_ts, entry_ts, variant):
    if entry_ts not in idx_by_ts: return None
    eidx=idx_by_ts[entry_ts]; entry=float(rows[eidx][1])
    atr=atr_at(rows,eidx); sup=support_at(rows,eidx)
    if atr is None or sup is None or atr<=0: return None
    anchor=sup if variant=='SUPPORT_ANCHORED' else entry
    sl=anchor-SL_ATR*atr; tp1=anchor+TP1_ATR*atr; tp2=anchor+TP2_ATR*atr
    remaining=1.0; realized=0.0; tp1_done=False
    last_idx=min(eidx+MAX_HOLD,len(rows)-1)
    for i in range(eidx+1,last_idx+1):
        h=float(rows[i][2]); l=float(rows[i][3])
        stop_hit=l<=sl
        tp1_hit=(not tp1_done) and h>=tp1
        tp2_hit=h>=tp2
        if stop_hit:
            realized += remaining*(sl/entry-1); remaining=0; return realized-COST, 'SL'
        if tp1_hit:
            realized += 0.5*(tp1/entry-1); remaining-=0.5; tp1_done=True
        if remaining>0 and tp2_hit:
            realized += remaining*(tp2/entry-1); remaining=0; return realized-COST, 'TP2'
    if remaining>0:
        px=float(rows[last_idx][4]); realized += remaining*(px/entry-1)
    return realized-COST, 'TIME'

now=dt.datetime.now(dt.timezone.utc); end_ms=int(now.timestamp()*1000); end_ms-=end_ms%BAR_MS
start_ms=end_ms-LOOKBACK_DAYS*86400000
results={v:[] for v in ('SUPPORT_ANCHORED','ENTRY_ANCHORED')}; reasons={v:{} for v in results}; failures={}
for s in SYMBOLS:
    try:
        oi=fetch_series('/futures/data/openInterestHist',s,start_ms,end_ms)
        tk=fetch_series('/futures/data/takerlongshortRatio',s,start_ms,end_ms)
        sp=fetch_spot(s,start_ms-2*86400000,end_ms+(48*60+15)*60000)
        om={int(r['timestamp']):r for r in oi}; tm={int(r['timestamp']):r for r in tk}; sm={int(r[0]):r for r in sp}; idx={int(r[0]):i for i,r in enumerate(sp)}
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
            entry_ts=ts+BAR_MS
            for v in results:
                sim=simulate(sp,idx,entry_ts,v)
                if sim is None: continue
                ret,reason=sim; results[v].append({'symbol':s,'entryTs':entry_ts,'ret':ret,'reason':reason})
                reasons[v][reason]=reasons[v].get(reason,0)+1
    except Exception as e: failures[s]=str(e)

report={'schemaVersion':1,'strategyId':'TST_DERIVATIVES_PRESSURE_V2_ATR_EXIT_RESEARCH','authorization':'RESEARCH_ONLY','liveTrading':False,'sourceConcept':'user-supplied TradingView Dynamic Trading Strategy with Key Levels, Entry/Exit Management','parameters':{'atrPeriod':ATR_N,'supportLookback':SUPPORT_N,'slAtr':SL_ATR,'tp1Atr':TP1_ATR,'tp2Atr':TP2_ATR,'partialExit':'50% TP1 / 50% TP2','maxHoldHours':48,'roundTripCost':COST,'intrabarRule':'stop-first if both touched'},'variants':{},'failures':failures,'generatedAt':now.isoformat()}
for v,trades in results.items():
    trades.sort(key=lambda x:x['entryTs']); vals=[t['ret'] for t in trades]; cut=int(len(vals)*0.7); allm=metrics(vals); oosm=metrics(vals[cut:]); report['variants'][v]={'all':allm,'oos30':oosm,'exitReasons':reasons[v],'passed':gate(allm) and gate(oosm)}
OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(report,indent=2)); print(json.dumps(report,indent=2))
