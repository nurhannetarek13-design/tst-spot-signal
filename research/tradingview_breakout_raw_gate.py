#!/usr/bin/env python3
from __future__ import annotations
import csv, datetime as dt, io, json, math, os, pathlib, statistics, urllib.request, zipfile

SYMBOLS=[s.strip().upper() for s in os.getenv('TV_BREAKOUT_SYMBOLS','BTCUSDT,ETHUSDT,SOLUSDT').split(',') if s.strip()]
MONTHS=int(os.getenv('TV_BREAKOUT_MONTHS','18'))
OUT=pathlib.Path(os.getenv('TV_BREAKOUT_OUT','/data/tradingview-breakout-raw-gate'))
BASE='https://data.binance.vision/data/spot/monthly/klines'
UA='tst-tv-breakout-research/1.0'


def month_seq(n):
    today=dt.date.today().replace(day=1)
    out=[]
    y,m=today.year,today.month
    for _ in range(n+1):
        m-=1
        if m==0:y-=1;m=12
        out.append(f'{y:04d}-{m:02d}')
    return list(reversed(out))


def norm_ms(x):
    v=int(float(x))
    return v//1000 if v>10**14 else v


def dl(symbol, month):
    url=f'{BASE}/{symbol}/15m/{symbol}-15m-{month}.zip'
    req=urllib.request.Request(url,headers={'User-Agent':UA})
    try:
        with urllib.request.urlopen(req,timeout=30) as r: raw=r.read()
    except Exception:
        return []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name=[n for n in zf.namelist() if n.lower().endswith('.csv')][0]
        rows=list(csv.reader(io.StringIO(zf.read(name).decode('utf-8-sig',errors='replace'))))
    out=[]
    for r in rows:
        if len(r)<7 or not str(r[0]).strip().replace('.','',1).isdigit(): continue
        try:
            out.append({'ts':norm_ms(r[0]),'open':float(r[1]),'high':float(r[2]),'low':float(r[3]),'close':float(r[4]),'volume':float(r[5])})
        except: pass
    return out


def summary(xs):
    v=[x for x in xs if x is not None and math.isfinite(x)]
    if not v:return {'n':0,'mean':None,'median':None,'hitRate':None}
    return {'n':len(v),'mean':sum(v)/len(v),'median':statistics.median(v),'hitRate':sum(x>0 for x in v)/len(v)}


def eval_symbol(symbol,bars):
    bars=sorted({b['ts']:b for b in bars}.values(), key=lambda x:x['ts'])
    hour={} 
    for b in bars:
        h=b['ts']//3600000
        q=hour.setdefault(h,{'high':b['high'],'low':b['low']})
        q['high']=max(q['high'],b['high']); q['low']=min(q['low'],b['low'])
    events=[]
    for i,b in enumerate(bars):
        h=b['ts']//3600000
        prev=hour.get(h-1)
        if not prev or i==0: continue
        p=bars[i-1]
        crossed=b['close']>prev['high'] and p['close']<=prev['high']
        if not crossed: continue
        e={'symbol':symbol,'ts':b['ts'],'entry':b['close'],'prev1hHigh':prev['high']}
        for hrs in (24,48,72):
            j=i+hrs*4
            e[f'fwd{hrs}h']=bars[j]['close']/b['close']-1 if j<len(bars) else None
        j2=min(len(bars),i+24*4+1)
        future=bars[i+1:j2]
        if future:
            mfe=max(x['high'] for x in future)/b['close']-1
            mae=b['close']/min(x['low'] for x in future)-1
            e['mfe24h']=mfe; e['mae24h']=mae; e['mfeMaeRatio']=mfe/mae if mae>0 else None
        else:e['mfe24h']=e['mae24h']=e['mfeMaeRatio']=None
        events.append(e)
    return events


def main():
    all_events=[]; coverage={}
    months=month_seq(MONTHS)
    for s in SYMBOLS:
        bars=[]
        for m in months: bars.extend(dl(s,m))
        ev=eval_symbol(s,bars); all_events.extend(ev)
        coverage[s]={'bars':len(bars),'events':len(ev)}
    by={}
    for s in SYMBOLS:
        ev=[e for e in all_events if e['symbol']==s]
        by[s]={h:summary([e.get(f'fwd{h}h') for e in ev]) for h in (24,48,72)}
        ratios=[e['mfeMaeRatio'] for e in ev if e.get('mfeMaeRatio') is not None]
        by[s]['medianMfeMae24h']=statistics.median(ratios) if ratios else None
    pooled={h:summary([e.get(f'fwd{h}h') for e in all_events]) for h in (24,48,72)}
    ratios=[e['mfeMaeRatio'] for e in all_events if e.get('mfeMaeRatio') is not None]
    pooled['medianMfeMae24h']=statistics.median(ratios) if ratios else None
    gate={'mean24hAtLeast1_5pct': pooled[24]['mean'] is not None and pooled[24]['mean']>=0.015,
          'median24hPositive': pooled[24]['median'] is not None and pooled[24]['median']>0,
          'hitRate24hAbove55pct': pooled[24]['hitRate'] is not None and pooled[24]['hitRate']>0.55,
          'mfeMaeAtLeast2': pooled['medianMfeMae24h'] is not None and pooled['medianMfeMae24h']>=2.0}
    gate['pass']=all(gate.values())
    OUT.mkdir(parents=True,exist_ok=True)
    csvp=OUT/'events.csv'; rep=OUT/'report.json'
    keys=['symbol','ts','entry','prev1hHigh','fwd24h','fwd48h','fwd72h','mfe24h','mae24h','mfeMaeRatio']
    with csvp.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=keys); w.writeheader(); w.writerows([{k:e.get(k) for k in keys} for e in all_events])
    payload={'authorization':'RESEARCH_ONLY','liveTrading':False,'source':'TradingView open-source 1H-15M breakout concept + Binance Vision Spot','symbols':SYMBOLS,'months':MONTHS,'coverage':coverage,'bySymbol':by,'pooled':pooled,'gate':gate,'definition':'Long when 15m close crosses above previous completed 1h high; signal at bar close; raw forward returns only; no TP/SL.'}
    rep.write_text(json.dumps(payload,indent=2),encoding='utf-8')
    print(json.dumps({'kind':'tv_breakout_raw_gate_complete','report':str(rep),'events':str(csvp),**payload},separators=(',',':')),flush=True)

if __name__=='__main__':main()
