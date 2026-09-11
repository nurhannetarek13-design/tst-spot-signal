#!/usr/bin/env python3
"""OOS validation for the only promising Round 7 hypothesis.

Frozen after Round 7 discovery:
DELEVERAGING_EXHAUSTION_V1:DOWN_LONG
- 1h price return <= -2.0%
- 1h open-interest-value return <= -5.0%
- 15m mean taker long/short volume ratio <= 0.75
- 4h forward horizon
- 12h event declustering

Uses an untouched PRE-discovery period: 2025-09-01 through 2026-03-01.
Research only; no optimization, costs, sizing or live authorization.
"""
from __future__ import annotations
import io,json,math,pathlib,time,urllib.request,zipfile
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import timedelta
import numpy as np
import pandas as pd

BASE='https://data.binance.vision/data/futures/um'
SYMBOLS=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT']
START=pd.Timestamp('2025-09-01T00:00:00Z'); END=pd.Timestamp('2026-03-01T00:00:00Z')
MIN_GAP=144; H=48; MIN_EVENTS=40
PRICE_1H=0.020; OI_DROP_1H=0.050; TAKER_LOW=0.75
WORKERS=24; RETRIES=3
OUT=pathlib.Path('validation/edges/deleveraging-exhaustion-oos-v1.json')
METRIC_COLS=['create_time','symbol','sum_open_interest','sum_open_interest_value','count_toptrader_long_short_ratio','sum_toptrader_long_short_ratio','count_long_short_ratio','sum_taker_long_short_vol_ratio']
KLINE_COLS=['open_time','open','high','low','close','volume','close_time','quote_volume','count','taker_buy_volume','taker_buy_quote_volume','ignore']

def fetch_bytes(url):
    last=None
    for k in range(RETRIES):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'tst-deleveraging-oos/1.0'})
            with urllib.request.urlopen(req,timeout=25) as r:return r.read()
        except Exception as e:last=e;time.sleep(.35*(2**k))
    raise RuntimeError(f'{url}: {last}')

def zip_csv(url,cols):
    raw=fetch_bytes(url)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:b=z.read(z.namelist()[0])
    txt=b.decode('utf-8-sig',errors='replace'); first=txt.splitlines()[0] if txt else ''
    header=bool(first and any(ch.isalpha() for ch in first.split(',')[0]))
    df=pd.read_csv(io.StringIO(txt),header=0 if header else None)
    if len(df.columns)!=len(cols):raise RuntimeError(f'{url}: columns={len(df.columns)} expected={len(cols)}')
    df.columns=cols;return df

def days():
    d=START.date();out=[]
    while d<END.date():out.append(d.isoformat());d+=timedelta(days=1)
    return out

def months():
    out=[];cur=START.tz_localize(None).to_period('M');end=END.tz_localize(None).to_period('M')
    while cur<end:out.append(f'{cur.year:04d}-{cur.month:02d}');cur+=1
    return out

def load_metrics(sym):
    urls=[f'{BASE}/daily/metrics/{sym}/{sym}-metrics-{d}.zip' for d in days()];frames=[];fails=0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs=[ex.submit(zip_csv,u,METRIC_COLS) for u in urls]
        for f in as_completed(futs):
            try:frames.append(f.result())
            except Exception:fails+=1
    if not frames:raise RuntimeError('no metrics')
    x=pd.concat(frames,ignore_index=True);x['create_time']=pd.to_datetime(x.create_time,utc=True,errors='coerce')
    for c in METRIC_COLS[2:]:x[c]=pd.to_numeric(x[c],errors='coerce')
    x=x.dropna(subset=['create_time']).drop_duplicates('create_time',keep='last').set_index('create_time').sort_index()
    grid=pd.date_range(START,END-pd.Timedelta(minutes=5),freq='5min');x=x.reindex(grid)
    return x,{'coverage':float(x.sum_open_interest_value.notna().mean()),'downloadFailures':fails}

def load_price(sym):
    frames=[]
    for ym in months():frames.append(zip_csv(f'{BASE}/monthly/klines/{sym}/5m/{sym}-5m-{ym}.zip',KLINE_COLS))
    x=pd.concat(frames,ignore_index=True);t=pd.to_numeric(x.open_time,errors='coerce');unit='us' if float(t.dropna().median())>1e14 else 'ms'
    x['ts']=pd.to_datetime(t,unit=unit,utc=True,errors='coerce')
    for c in ['open','high','low','close']:x[c]=pd.to_numeric(x[c],errors='coerce')
    x=x.dropna(subset=['ts']).drop_duplicates('ts',keep='last').set_index('ts').sort_index()
    grid=pd.date_range(START,END-pd.Timedelta(minutes=5),freq='5min');x=x.reindex(grid)
    return x[['open','high','low','close']],{'coverage':float(x.close.notna().mean())}

def decluster(ix):
    out=[];last=-10**18
    for i in sorted(ix):
        if i-last>=MIN_GAP:out.append(i);last=i
    return out

def main():
    events=[];quality={};excluded={}
    for sym in SYMBOLS:
        try:m,mq=load_metrics(sym);p,pq=load_price(sym)
        except Exception as e:excluded[sym]=str(e);continue
        quality[sym]={'metrics':mq,'price':pq}
        if mq['coverage']<.985 or pq['coverage']<.999:excluded[sym]='DATA_QUALITY_GATE';continue
        x=p.join(m,how='left');x['r1h']=x.close.pct_change(12,fill_method=None);x['oi1h']=x.sum_open_interest_value.pct_change(12,fill_method=None);x['taker15']=x.sum_taker_long_short_vol_ratio.rolling(3,min_periods=3).mean()
        sig=(x.r1h<=-PRICE_1H)&(x.oi1h<=-OI_DROP_1H)&(x.taker15<=TAKER_LOW)
        ix=decluster(np.flatnonzero(sig.fillna(False).to_numpy()).tolist())
        price=x[['open','high','low','close']]
        events.extend((sym,price,i) for i in ix)
    vals=[];mfe=[];mae=[];by_symbol={s:[] for s in SYMBOLS}
    for sym,df,i in events:
        if i+H>=len(df):continue
        e=float(df.close.iloc[i]);f=float(df.close.iloc[i+H])
        if not np.isfinite(e) or not np.isfinite(f) or e<=0:continue
        r=f/e-1;vals.append(r);by_symbol[sym].append(r)
        seg=df.iloc[i+1:i+H+1];mfe.append(float(seg.high.max()/e-1));mae.append(float(max(0,1-seg.low.min()/e)))
    a=np.asarray(vals,float);m=np.asarray(mfe,float);d=np.asarray(mae,float)
    if len(a):
        med_mae=float(np.median(d));ratio=float(np.median(m)/med_mae) if med_mae>0 else float('inf')
        mean=float(a.mean());median=float(np.median(a));hit=float((a>0).mean())
        if len(a)>=2:
            sd=float(a.std(ddof=1));z=mean/(sd/math.sqrt(len(a))) if sd>0 else float('inf');p=0.5*math.erfc(z/math.sqrt(2)) if sd>0 else 0.0
        else:p=1.0
    else:mean=median=hit=ratio=None;p=1.0
    gate=bool(len(a)>=MIN_EVENTS and mean is not None and mean>=.015 and median>0 and hit>.55 and ratio>=2.0 and p<=.05)
    per_symbol={s:{'n':len(v),'mean':(float(np.mean(v)) if v else None),'hitRate':(float(np.mean(np.asarray(v)>0)) if v else None)} for s,v in by_symbol.items()}
    report={'engine':'DELEVERAGING_EXHAUSTION_OOS_V1','authorization':'RESEARCH_ONLY','liveTrading':False,'liveReady':False,
      'sourceHypothesis':'ROUND7_DELEVERAGING_EXHAUSTION_V1_DOWN_LONG_4H','discoveryPeriodExcluded':{'start':'2026-03-01','endExclusive':'2026-09-01'},
      'oosPeriod':{'start':str(START),'endExclusive':str(END)},'symbols':SYMBOLS,'symbolsExcluded':excluded,'dataQuality':quality,
      'frozenDefinition':{'priceReturn1hLTE':-PRICE_1H,'oiValueReturn1hLTE':-OI_DROP_1H,'taker15LTE':TAKER_LOW,'horizonBars5m':H,'declusterBars5m':MIN_GAP},
      'gate':{'minimumEvents':MIN_EVENTS,'meanMin':.015,'medianPositive':True,'hitRateMinExclusive':.55,'mfeMaeMin':2.0,'pMeanPositiveMax':.05},
      'result':{'n':int(len(a)),'mean':mean,'median':median,'hitRate':hit,'medianMFE':(float(np.median(m)) if len(m) else None),'medianMAE':(float(np.median(d)) if len(d) else None),'mfeMaeRatio':ratio,'pMeanPositive':p,'perSymbol':per_symbol},
      'decision':'OOS_SURVIVOR' if gate else ('INSUFFICIENT_OOS' if len(a)<MIN_EVENTS else 'REJECT_OOS')}
    OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(report,indent=2,sort_keys=True));print(json.dumps(report,indent=2,sort_keys=True))
if __name__=='__main__':main()
