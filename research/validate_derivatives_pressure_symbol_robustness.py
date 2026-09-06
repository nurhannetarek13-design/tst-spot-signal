#!/usr/bin/env python3
"""Symbol robustness validation for frozen derivatives-pressure 48h strategy.

No tuning. Rebuilds the frozen event/execution definition, then tests leave-one-symbol-out
portfolio robustness, per-symbol dispersion, and contribution concentration. Research only.
"""
import datetime as dt, json, pathlib, statistics, time, urllib.parse, urllib.request

SPOT='https://data-api.binance.vision'; WWW='https://www.binance.com'
OUT=pathlib.Path('validation/edges/derivatives-pressure-48h-symbol-robustness.json')
PERIOD='15m'; BAR_MS=15*60*1000; LOOKBACK_DAYS=14; H48=192
SYMBOLS=['PUMPUSDT','ASTERUSDT','WLDUSDT','ENAUSDT','NEARUSDT','SUIUSDT','ARBUSDT','MARSCOINUSDT','ZKCUSDT','SAHARAUSDT']
BASE_COST=0.002; STRESS_COST=0.006

def get(base,path):
    req=urllib.request.Request(base+path,headers={'User-Agent':'Mozilla/5.0 tst-symbol-robustness/1.0','Accept':'application/json,text/plain,*/*','Referer':'https://www.binance.com/'})
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
def metrics(vals):
    if not vals: return {'n':0,'meanPct':None,'medianPct':None,'hitRatePct':None,'profitFactor':None}
    wins=sum(v for v in vals if v>0); losses=-sum(v for v in vals if v<0)
    return {'n':len(vals),'meanPct':round(100*sum(vals)/len(vals),4),'medianPct':round(100*statistics.median(vals),4),'hitRatePct':round(100*sum(v>0 for v in vals)/len(vals),2),'profitFactor':None if losses<=0 else round(wins/losses,4)}
def gate(m,min_n=30):
    return m['n']>=min_n and m['meanPct'] is not None and m['meanPct']>=1.0 and m['medianPct']>0 and m['hitRatePct']>55 and m['profitFactor'] is not None and m['profitFactor']>1.2

now=dt.datetime.now(dt.timezone.utc); end_ms=int(now.timestamp()*1000); end_ms-=end_ms%BAR_MS; start_ms=end_ms-LOOKBACK_DAYS*86400000
trades=[]; failures={}
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
            if oi0<=0 or any(ts-j*BAR_MS not in tm for j in range(4)): prev=False; continue
            oichg=oi1/oi0-1; tr=sum(float(tm[ts-j*BAR_MS].get('buySellRatio') or 1) for j in range(4))/4
            score=50+(20 if oichg>=0.02 else 10 if oichg>0 else -10)+(20 if tr>=1.15 else 10 if tr>=1.05 else -10)
            cond=score>=80 and oichg>0 and tr>=1.05; onset=cond and not prev; prev=cond
            if not onset: continue
            entry_ts=ts+BAR_MS; exit_ts=entry_ts+H48*BAR_MS
            if entry_ts not in sm or exit_ts not in sm: continue
            gross=float(sm[exit_ts][4])/float(sm[entry_ts][1])-1
            trades.append({'symbol':s,'entryTs':entry_ts,'base':gross-BASE_COST,'stress':gross-STRESS_COST})
    except Exception as e: failures[s]=str(e)

trades.sort(key=lambda x:x['entryTs'])
per_symbol={}
for s in SYMBOLS:
    xs=[t['base'] for t in trades if t['symbol']==s]
    per_symbol[s]=metrics(xs)

loso={}; loso_pass=True
for held in SYMBOLS:
    xs=[t['base'] for t in trades if t['symbol']!=held]
    m=metrics(xs); p=gate(m,30); loso[held]={'metrics':m,'passed':p}; loso_pass=loso_pass and p

# Chronological OOS on every LOSO portfolio to make sure robustness survives time split too.
loso_oos={}; loso_oos_pass=True
for held in SYMBOLS:
    pool=[t for t in trades if t['symbol']!=held]; cut=int(len(pool)*0.7); xs=[t['base'] for t in pool[cut:]]
    m=metrics(xs); p=gate(m,10); loso_oos[held]={'metrics':m,'passed':p}; loso_oos_pass=loso_oos_pass and p

base_total=sum(t['base'] for t in trades)
pos_contrib={s:sum(t['base'] for t in trades if t['symbol']==s) for s in SYMBOLS}
sorted_pos=sorted(((s,v) for s,v in pos_contrib.items() if v>0),key=lambda z:z[1],reverse=True)
top1_share=None if base_total<=0 or not sorted_pos else sorted_pos[0][1]/base_total
top2_share=None if base_total<=0 else sum(v for _,v in sorted_pos[:2])/base_total

all_base=metrics([t['base'] for t in trades]); all_stress=metrics([t['stress'] for t in trades])
negative_symbols=[s for s,m in per_symbol.items() if m['n']>=10 and (m['meanPct'] is None or m['meanPct']<=0)]
weak_symbols=[s for s,m in per_symbol.items() if m['n']>=10 and not (m['meanPct']>0 and m['medianPct']>0 and m['hitRatePct']>55)]
concentration_pass=(top1_share is not None and top1_share<0.50 and top2_share is not None and top2_share<0.75)
validation={
 'passed': gate(all_base,50) and gate(all_stress,50) and loso_pass and loso_oos_pass and concentration_pass,
 'allBasePassed':gate(all_base,50),'allStressPassed':gate(all_stress,50),
 'allLeaveOneOutPassed':loso_pass,'allLeaveOneOutOosPassed':loso_oos_pass,
 'concentrationPassed':concentration_pass
}
report={'schemaVersion':1,'strategyId':'TST_DERIVATIVES_PRESSURE_V2_48H_EXECUTABLE_V1','validationType':'SYMBOL_ROBUSTNESS_LOSO','authorization':'RESEARCH_ONLY','liveTrading':False,'tradeCount':len(trades),'allBase':all_base,'allStress60bps':all_stress,'perSymbol':per_symbol,'leaveOneSymbolOut':loso,'leaveOneSymbolOutOos30':loso_oos,'negativeSymbols':negative_symbols,'weakSymbols':weak_symbols,'contributionConcentration':{'top1Share':None if top1_share is None else round(top1_share,4),'top2Share':None if top2_share is None else round(top2_share,4),'positiveContributionRanking':[(s,round(v,6)) for s,v in sorted_pos]},'validation':validation,'failures':failures,'generatedAt':now.isoformat()}
OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(report,indent=2)); print(json.dumps(report,indent=2))
