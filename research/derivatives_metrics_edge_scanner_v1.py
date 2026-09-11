#!/usr/bin/env python3
"""Round 7: frozen derivatives-positioning raw-edge discovery.

Uses Binance Vision USD-M futures 5m metrics + 5m contract klines + funding.
Predeclared families before results:
1) DELEVERAGING_EXHAUSTION_V1
2) LEVERAGED_CONTINUATION_V1
3) SMART_CROWD_DIVERGENCE_V1
4) FUNDING_CROWDING_SQUEEZE_V1

Research only. No strategy rules, costs, sizing, optimization, or live authorization.
"""
from __future__ import annotations
import io, json, math, pathlib, time, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
import numpy as np
import pandas as pd

BASE='https://data.binance.vision/data/futures/um'
SYMBOLS=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT']
START=pd.Timestamp('2026-03-01T00:00:00Z'); END=pd.Timestamp('2026-09-01T00:00:00Z')
INTERVAL='5m'; HORIZONS={'4h':48,'12h':144,'24h':288}
MIN_EVENTS=40; MIN_GAP=144; FDR_Q=0.05
METRICS_MIN_COVERAGE=0.985; METRICS_MAX_DUP_RATE=0.001; PRICE_MIN_COVERAGE=0.999
WORKERS=24; RETRIES=3
OUT=pathlib.Path('validation/edges/derivatives-metrics-edge-scanner-v1-latest.json')

# Frozen thresholds.
EXH_PRICE_1H=0.020; EXH_OI_1H=0.050; EXH_TAKER_LOW=0.75; EXH_TAKER_HIGH=1/EXH_TAKER_LOW
CONT_PRICE_1H=0.015; CONT_OI_1H=0.040; CONT_TAKER_LOW=0.80; CONT_TAKER_HIGH=1/CONT_TAKER_LOW
DIV_TOP_LONG=1.25; DIV_TOP_SHORT=0.80; DIV_CROWD_SHORT=0.85; DIV_CROWD_LONG=1.18
FUND_EXTREME=0.00030; FUND_CROWD_SHORT=0.80; FUND_CROWD_LONG=1.25; FUND_OI_4H=0.03

METRIC_COLS=['create_time','symbol','sum_open_interest','sum_open_interest_value','count_toptrader_long_short_ratio','sum_toptrader_long_short_ratio','count_long_short_ratio','sum_taker_long_short_vol_ratio']
KLINE_COLS=['open_time','open','high','low','close','volume','close_time','quote_volume','count','taker_buy_volume','taker_buy_quote_volume','ignore']
FUND_COLS=['calc_time','funding_interval_hours','last_funding_rate']

def log(s): print(f"[{time.strftime('%H:%M:%S')}] {s}",flush=True)

def fetch_bytes(url):
    last=None
    for k in range(RETRIES):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'tst-round7-derivatives/1.0'})
            with urllib.request.urlopen(req,timeout=25) as r:return r.read()
        except Exception as e:
            last=e; time.sleep(0.35*(2**k))
    raise RuntimeError(f'{url}: {last}')

def zip_csv(url, cols=None):
    raw=fetch_bytes(url)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name=z.namelist()[0]; b=z.read(name)
    # Recent metrics/funding usually have headers; kline header behavior varies. Sniff safely.
    txt=b.decode('utf-8-sig',errors='replace')
    first=txt.splitlines()[0] if txt else ''
    has_header=bool(first and any(ch.isalpha() for ch in first.split(',')[0]))
    df=pd.read_csv(io.StringIO(txt),header=0 if has_header else None)
    if cols is not None:
        if len(df.columns)!=len(cols): raise RuntimeError(f'{url}: columns={len(df.columns)} expected={len(cols)}')
        df.columns=cols
    return df

def months():
    out=[]; cur=START.to_period('M'); end=END.to_period('M')
    while cur<end:
        out.append(f'{cur.year:04d}-{cur.month:02d}'); cur+=1
    return out

def days():
    d=START.date(); end=END.date(); out=[]
    while d<end: out.append(d.isoformat()); d+=timedelta(days=1)
    return out

def load_metrics(sym):
    urls=[f'{BASE}/daily/metrics/{sym}/{sym}-metrics-{d}.zip' for d in days()]
    frames=[]; fail=[]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        fut={ex.submit(zip_csv,u,METRIC_COLS):u for u in urls}
        for f in as_completed(fut):
            try: frames.append(f.result())
            except Exception as e: fail.append(str(e))
    if not frames: raise RuntimeError(f'{sym}: no metrics')
    x=pd.concat(frames,ignore_index=True)
    x['create_time']=pd.to_datetime(x.create_time,utc=True,errors='coerce')
    for c in METRIC_COLS[2:]: x[c]=pd.to_numeric(x[c],errors='coerce')
    x=x.dropna(subset=['create_time']).sort_values('create_time')
    dup=int(x.duplicated('create_time').sum()); x=x.drop_duplicates('create_time',keep='last').set_index('create_time')
    grid=pd.date_range(START,END-pd.Timedelta(minutes=5),freq='5min')
    x=x.reindex(grid)
    coverage=float(x['sum_open_interest_value'].notna().mean())
    dup_rate=dup/max(1,len(x))
    quality={'coverage':coverage,'duplicates':dup,'duplicateRate':dup_rate,'downloadFailures':len(fail)}
    return x,quality

def load_klines(sym):
    frames=[]
    for ym in months():
        u=f'{BASE}/monthly/klines/{sym}/5m/{sym}-5m-{ym}.zip'; frames.append(zip_csv(u,KLINE_COLS))
    x=pd.concat(frames,ignore_index=True)
    t=pd.to_numeric(x.open_time,errors='coerce'); unit='us' if float(t.dropna().median())>1e14 else 'ms'
    x['ts']=pd.to_datetime(t,unit=unit,utc=True,errors='coerce')
    for c in ['open','high','low','close']: x[c]=pd.to_numeric(x[c],errors='coerce')
    x=x.dropna(subset=['ts']).drop_duplicates('ts',keep='last').set_index('ts').sort_index()
    grid=pd.date_range(START,END-pd.Timedelta(minutes=5),freq='5min')
    x=x.reindex(grid)
    coverage=float(x.close.notna().mean())
    return x[['open','high','low','close']],{'coverage':coverage}

def load_funding(sym):
    frames=[]
    for ym in months():
        u=f'{BASE}/monthly/fundingRate/{sym}/{sym}-fundingRate-{ym}.zip'
        try: frames.append(zip_csv(u,FUND_COLS))
        except Exception: pass
    if not frames:return pd.DataFrame(columns=['funding'])
    x=pd.concat(frames,ignore_index=True); t=pd.to_numeric(x.calc_time,errors='coerce')
    x['ts']=pd.to_datetime(t,unit='ms',utc=True,errors='coerce'); x['funding']=pd.to_numeric(x.last_funding_rate,errors='coerce')
    return x.dropna(subset=['ts']).drop_duplicates('ts',keep='last').set_index('ts')[['funding']].sort_index()

def decluster(ix):
    out=[]; last=-10**18
    for i in sorted(ix):
        if i-last>=MIN_GAP: out.append(i); last=i
    return out

def pmean(a):
    if len(a)<2:return 1.0
    sd=float(a.std(ddof=1))
    if sd<=0:return 0.0 if float(a.mean())>0 else 1.0
    z=float(a.mean())/(sd/math.sqrt(len(a)))
    return 0.5*math.erfc(z/math.sqrt(2))

def event_stats(events,h):
    vals=[]; mfe=[]; mae=[]
    for df,i,sign in events:
        if i+h>=len(df):continue
        e=float(df.close.iloc[i]); f=float(df.close.iloc[i+h]);
        if not np.isfinite(e) or not np.isfinite(f) or e<=0:continue
        vals.append((f/e-1)*sign)
        hi=float(df.high.iloc[i+1:i+h+1].max())/e-1; lo=1-float(df.low.iloc[i+1:i+h+1].min())/e
        if sign>0: mfe.append(hi); mae.append(max(0.0,lo))
        else: mfe.append(lo); mae.append(max(0.0,hi))
    if not vals:return None
    a=np.asarray(vals); m=np.asarray(mfe); d=np.asarray(mae); med_mae=float(np.median(d)); ratio=float(np.median(m))/med_mae if med_mae>0 else float('inf')
    mean=float(a.mean()); med=float(np.median(a)); hit=float((a>0).mean())
    return {'n':len(a),'mean':mean,'median':med,'hitRate':hit,'medianMFE':float(np.median(m)),'medianMAE':med_mae,'mfeMaeRatio':ratio,'pMeanPositive':pmean(a),'rawPass':bool(len(a)>=MIN_EVENTS and mean>=0.015 and med>0 and hit>0.55 and ratio>=2.0)}

def bh(rows):
    ps=sorted([(i,r['pMeanPositive']) for i,r in enumerate(rows)],key=lambda z:z[1]); m=len(ps); adj=[1.0]*m; run=1.0
    for rank in range(m,0,-1):
        i,p=ps[rank-1]; run=min(run,p*m/rank); adj[i]=min(1.0,run)
    for i,r in enumerate(rows):r['qValue']=adj[i];r['pass']=bool(r['rawPass'] and adj[i]<=FDR_Q)

def main():
    t0=time.time(); pools={
      'DELEVERAGING_EXHAUSTION_V1:DOWN_LONG':[],'DELEVERAGING_EXHAUSTION_V1:UP_SHORT':[],
      'LEVERAGED_CONTINUATION_V1:UP_LONG':[],'LEVERAGED_CONTINUATION_V1:DOWN_SHORT':[],
      'SMART_CROWD_DIVERGENCE_V1:TOP_LONG':[],'SMART_CROWD_DIVERGENCE_V1:TOP_SHORT':[],
      'FUNDING_CROWDING_SQUEEZE_V1:NEG_LONG':[],'FUNDING_CROWDING_SQUEEZE_V1:POS_SHORT':[]}
    quality={}; excluded={}
    for sym in SYMBOLS:
        log(f'LOAD {sym}')
        try:m,mq=load_metrics(sym); p,pq=load_klines(sym); f=load_funding(sym)
        except Exception as e: excluded[sym]=str(e); continue
        quality[sym]={'metrics':mq,'price':pq,'fundingEvents':len(f)}
        if mq['coverage']<METRICS_MIN_COVERAGE or mq['duplicateRate']>METRICS_MAX_DUP_RATE or pq['coverage']<PRICE_MIN_COVERAGE:
            excluded[sym]='DATA_QUALITY_GATE'; continue
        x=p.join(m,how='left')
        x['funding']=np.nan
        if len(f):
            # Funding is known only at settlement timestamp; forward-use last known rate, capped implicitly by event availability.
            tmp=pd.merge_asof(x.reset_index().rename(columns={'index':'ts'}).sort_values('ts'),f.reset_index().sort_values('ts'),on='ts',direction='backward',tolerance=pd.Timedelta(hours=9))
            x=tmp.set_index('ts')
        x['r1h']=x.close.pct_change(12,fill_method=None); x['oi1h']=x.sum_open_interest_value.pct_change(12,fill_method=None); x['oi4h']=x.sum_open_interest_value.pct_change(48,fill_method=None)
        x['taker15']=x.sum_taker_long_short_vol_ratio.rolling(3,min_periods=3).mean()
        x['top15']=x.sum_toptrader_long_short_ratio.rolling(3,min_periods=3).mean(); x['crowd15']=x.count_long_short_ratio.rolling(3,min_periods=3).mean()
        defs={
          'DELEVERAGING_EXHAUSTION_V1:DOWN_LONG':((x.r1h<=-EXH_PRICE_1H)&(x.oi1h<=-EXH_OI_1H)&(x.taker15<=EXH_TAKER_LOW),1),
          'DELEVERAGING_EXHAUSTION_V1:UP_SHORT':((x.r1h>=EXH_PRICE_1H)&(x.oi1h<=-EXH_OI_1H)&(x.taker15>=EXH_TAKER_HIGH),-1),
          'LEVERAGED_CONTINUATION_V1:UP_LONG':((x.r1h>=CONT_PRICE_1H)&(x.oi1h>=CONT_OI_1H)&(x.taker15>=CONT_TAKER_HIGH),1),
          'LEVERAGED_CONTINUATION_V1:DOWN_SHORT':((x.r1h<=-CONT_PRICE_1H)&(x.oi1h>=CONT_OI_1H)&(x.taker15<=CONT_TAKER_LOW),-1),
          'SMART_CROWD_DIVERGENCE_V1:TOP_LONG':((x.top15>=DIV_TOP_LONG)&(x.crowd15<=DIV_CROWD_SHORT),1),
          'SMART_CROWD_DIVERGENCE_V1:TOP_SHORT':((x.top15<=DIV_TOP_SHORT)&(x.crowd15>=DIV_CROWD_LONG),-1),
          'FUNDING_CROWDING_SQUEEZE_V1:NEG_LONG':((x.funding<=-FUND_EXTREME)&(x.crowd15<=FUND_CROWD_SHORT)&(x.oi4h>=FUND_OI_4H),1),
          'FUNDING_CROWDING_SQUEEZE_V1:POS_SHORT':((x.funding>=FUND_EXTREME)&(x.crowd15>=FUND_CROWD_LONG)&(x.oi4h>=FUND_OI_4H),-1)}
        price=x[['open','high','low','close']]
        for name,(sig,sign) in defs.items():
            ix=decluster(np.flatnonzero(sig.fillna(False).to_numpy()).tolist()); pools[name].extend((price,i,sign) for i in ix)
        log(f'{sym} OK coverage={mq["coverage"]:.4f}')
    tests=[]
    for family,ev in pools.items():
        log(f'{family} events={len(ev)}')
        for label,h in HORIZONS.items():
            st=event_stats(ev,h)
            if st:tests.append({'family':family,'horizon':label,'bars':h,**st})
    bh(tests); survivors=sorted([r for r in tests if r['pass']],key=lambda r:(r['qValue'],-r['mean']))
    report={'engine':'DERIVATIVES_METRICS_EDGE_SCANNER_V1','round':7,'authorization':'RESEARCH_ONLY','liveTrading':False,
      'period':{'start':str(START),'endExclusive':str(END)},'symbols':SYMBOLS,'symbolsExcluded':excluded,'dataQuality':quality,
      'dataQualityGate':{'metricsMinCoverage':METRICS_MIN_COVERAGE,'metricsMaxDuplicateRate':METRICS_MAX_DUP_RATE,'priceMinCoverage':PRICE_MIN_COVERAGE,'missingValues':'NO_SIGNAL_NO_FILL'},
      'families':['DELEVERAGING_EXHAUSTION_V1','LEVERAGED_CONTINUATION_V1','SMART_CROWD_DIVERGENCE_V1','FUNDING_CROWDING_SQUEEZE_V1'],
      'frozenDefinitions':{'DELEVERAGING_EXHAUSTION_V1':{'price1hAbs':EXH_PRICE_1H,'oiValueDrop1h':EXH_OI_1H,'takerLow':EXH_TAKER_LOW,'takerHigh':EXH_TAKER_HIGH},
       'LEVERAGED_CONTINUATION_V1':{'price1hAbs':CONT_PRICE_1H,'oiValueRise1h':CONT_OI_1H,'takerLow':CONT_TAKER_LOW,'takerHigh':CONT_TAKER_HIGH},
       'SMART_CROWD_DIVERGENCE_V1':{'topLong':DIV_TOP_LONG,'topShort':DIV_TOP_SHORT,'crowdShort':DIV_CROWD_SHORT,'crowdLong':DIV_CROWD_LONG},
       'FUNDING_CROWDING_SQUEEZE_V1':{'fundingAbs':FUND_EXTREME,'crowdShort':FUND_CROWD_SHORT,'crowdLong':FUND_CROWD_LONG,'oiValueRise4h':FUND_OI_4H}},
      'horizons':list(HORIZONS),'declusterBars':MIN_GAP,'rawGate':{'minEvents':MIN_EVENTS,'mean':0.015,'median':'>0','hitRate':'>0.55','mfeMaeRatio':2.0},
      'multipleTesting':{'method':'Benjamini-Hochberg','q':FDR_Q},'eventCounts':{k:len(v) for k,v in pools.items()},'tests':len(tests),'results':tests,'survivorCount':len(survivors),'survivors':survivors,'liveReady':False,'runtimeSeconds':round(time.time()-t0,1)}
    OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(report,indent=2,sort_keys=True));print(json.dumps(report,indent=2,sort_keys=True))
if __name__=='__main__':main()
