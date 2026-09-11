#!/usr/bin/env python3
"""Gate F V2: exact frozen breadth/lead-lag test using Spot klines only.
RESEARCH_ONLY. No ML, sizing, fees, or live execution.
"""
from __future__ import annotations
import json,pathlib,time,urllib.parse,urllib.request
from datetime import datetime,timedelta,timezone
import numpy as np,pandas as pd
AUTH='RESEARCH_ONLY'
BASE='https://data-api.binance.vision'
SYMS=('ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ADAUSDT')
DISC=('ETHUSDT','SOLUSDT','BNBUSDT'); HOLD=('XRPUSDT','DOGEUSDT','ADAUSDT')
EVENTS=('btc_leads_breadth_lag','broad_risk_on_continuation','breadth_reacceleration')
HOURS=(48,72,168); BARS={h:h*4 for h in HOURS}

def api(path):
    req=urllib.request.Request(BASE+path,headers={'User-Agent':'gate-f-v2/1.0'})
    with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)

def klines(sym,start,end):
    cur=int(datetime.combine(start,datetime.min.time(),tzinfo=timezone.utc).timestamp()*1000)
    stop=int((datetime.combine(end,datetime.min.time(),tzinfo=timezone.utc)+timedelta(days=1)).timestamp()*1000)-1
    rows=[]
    while cur<=stop:
        q=urllib.parse.urlencode({'symbol':sym,'interval':'15m','limit':1000,'startTime':cur,'endTime':stop})
        b=api('/api/v3/klines?'+q)
        if not b:break
        rows+=b;nxt=int(b[-1][0])+900000
        if nxt<=cur:break
        cur=nxt;time.sleep(.03)
    c=['ot','o','h','l','c','v','ct','qv','n','tb','tq','x'];d=pd.DataFrame(rows,columns=c)
    for x in ['h','l','c']:d[x]=pd.to_numeric(d[x],errors='coerce')
    d['ts']=pd.to_datetime(d.ot,unit='ms',utc=True)
    return d[['ts','h','l','c']].drop_duplicates('ts').sort_values('ts').reset_index(drop=True)

def dedup(rows,h):
    rows=sorted(rows,key=lambda x:x['ts']);out=[];last=None
    for r in rows:
        ts=pd.Timestamp(r['ts'])
        if last is None or (ts-last).total_seconds()>=h*3600:out.append(r);last=ts
    return out

def stats(rows):
    if not rows:return {'n':0,'meanPct':None,'medianPct':None,'hitRate':None,'mfeMaeRatio':None}
    r=np.array([x['ret'] for x in rows],float);mfe=np.array([x['mfe'] for x in rows],float);mae=np.array([x['mae'] for x in rows],float)
    mmfe=float(np.median(mfe));mmae=float(np.median(mae))
    return {'n':len(rows),'meanPct':float(r.mean()*100),'medianPct':float(np.median(r)*100),'hitRate':float((r>0).mean()),'mfeMaeRatio':float(mmfe/mmae) if mmae>0 else None}

def passes(st,minn):
    return bool(st['n']>=minn and st['qualifyingSymbols']>=2 and st['meanPct']>=1.5 and st['medianPct']>0 and st['hitRate']>.55 and (st['mfeMaeRatio'] or 0)>=2)

def main():
    latest=(datetime.now(timezone.utc)-timedelta(days=2)).date();core_start=latest-timedelta(days=89);load_start=core_start-timedelta(days=8);load_end=latest+timedelta(days=7)
    frames={s:klines(s,load_start,load_end) for s in ('BTCUSDT',)+SYMS}
    panel=frames['BTCUSDT'][['ts','c']].rename(columns={'c':'btc'})
    for s in SYMS:panel=panel.merge(frames[s][['ts','c']].rename(columns={'c':s}),on='ts',how='inner')
    panel=panel.sort_values('ts').reset_index(drop=True);panel['btc_r24']=panel.btc.pct_change(96)
    pos=[(panel[s].pct_change(96)>0).astype(float).rename(s) for s in SYMS]
    panel['breadth']=pd.concat(pos,axis=1).mean(axis=1);panel['breadth_delta_6h']=panel.breadth-panel.breadth.shift(24)
    panel['btc_leads_breadth_lag']=(panel.btc_r24>=.02)&(panel.breadth<=.34)
    panel['broad_risk_on_continuation']=(panel.btc_r24>=.015)&(panel.breadth>=.80)
    panel['breadth_reacceleration']=(panel.btc_r24>=0)&(panel.breadth>=.67)&(panel.breadth_delta_6h>=.34)
    by={ev:{s:{str(h):[] for h in HOURS} for s in SYMS} for ev in EVENTS}
    for s in SYMS:
        d=frames[s].merge(panel[['ts']+list(EVENTS)],on='ts',how='inner').reset_index(drop=True)
        for ev in EVENTS:
            idx=np.flatnonzero(d[ev].fillna(False).to_numpy())
            for h in HOURS:
                n=BARS[h]
                for i in idx:
                    ts=pd.Timestamp(d.ts.iloc[i]);day=ts.date()
                    if not(core_start<=day<=latest) or i+n>=len(d):continue
                    e=float(d.c.iloc[i]);seg=d.iloc[i+1:i+n+1]
                    by[ev][s][str(h)].append({'ts':ts.isoformat(),'ret':float(d.c.iloc[i+n]/e-1),'mfe':float(seg.h.max()/e-1),'mae':float(max(0,1-seg.l.min()/e))})
    results={};w=[]
    for ev in EVENTS:
        results[ev]={}
        for h in HOURS:
            sts={}
            for label,syms,minn in [('discovery',DISC,18),('symbolHoldout',HOLD,15)]:
                pooled=[];q=0
                for s in syms:
                    rows=dedup(by[ev][s][str(h)],h)
                    if len(rows)>=3:q+=1
                    pooled+=rows
                st=stats(pooled);st['qualifyingSymbols']=q;sts[label]=st
            ok=passes(sts['discovery'],18) and passes(sts['symbolHoldout'],15)
            results[ev][str(h)]={**sts,'rawGatePass':ok}
            if ok:w.append({'event':ev,'horizonHours':h})
    out={'engine':'GATE_F_BREADTH_DISCOVERY_V2','authorization':AUTH,'liveTrading':False,'usesML':False,'usesSizing':False,'thresholdsFrozen':True,'period':{'start':core_start.isoformat(),'end':latest.isoformat()},'discoverySymbols':list(DISC),'holdoutSymbols':list(HOLD),'results':results,'winners':w,'next':'FREEZE_BREADTH_EVENT_AND_TIME_OOS' if w else 'REJECT_GATE_F_V2'}
    p=pathlib.Path('validation/edges/gate-f-breadth-v2.json');p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(out,indent=2));print(json.dumps({'winners':w,'next':out['next'],'period':out['period']}))
if __name__=='__main__':main()
