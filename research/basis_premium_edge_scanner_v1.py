#!/usr/bin/env python3
"""Round 8: frozen USD-M basis/premium dislocation discovery.

Predeclared before results. Uses Binance Vision USD-M 5m premium-index klines,
5m contract klines, and 5m derivatives metrics. Research only.

Families:
1) PREMIUM_TS_REVERSION_V1
2) PREMIUM_OI_SQUEEZE_V1
3) CROSS_SECTIONAL_PREMIUM_EXTREME_V1

No strategy optimization, sizing, costs, or live authorization.
"""
from __future__ import annotations
import io, json, math, pathlib, time, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
import numpy as np
import pandas as pd

BASE='https://data.binance.vision/data/futures/um'
SYMBOLS=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT']
START=pd.Timestamp('2026-03-01T00:00:00Z'); END=pd.Timestamp('2026-09-01T00:00:00Z')
INTERVAL='5m'; HORIZONS={'4h':48,'12h':144,'24h':288}
MIN_EVENTS=40; MIN_GAP=144; FDR_Q=0.05
METRICS_MIN_COVERAGE=0.985; PRICE_MIN_COVERAGE=0.999; PREMIUM_MIN_COVERAGE=0.999
WORKERS=24; RETRIES=3
OUT=pathlib.Path('validation/edges/basis-premium-edge-scanner-v1-latest.json')

# Frozen, pre-result definitions.
PREM_Z_LOOKBACK=2016       # 7 days of 5m observations
PREM_Z_ABS=2.25
PREM_TAKER_LONG_MAX=0.90
PREM_TAKER_SHORT_MIN=1.10
PREM_OI_4H_MIN=0.03
PREM_OI_Z_ABS=2.00
CS_BOTTOM_Q=1/6
CS_TOP_Q=5/6

# Same hard economics gate as prior rounds plus cross-symbol robustness.
MIN_MEAN=0.015; MIN_HIT=0.55; MIN_MFE_MAE=2.0
MIN_POSITIVE_SYMBOLS=4; MAX_EVENT_SHARE=0.40
LOO_MIN_MEAN=0.0; LOO_MIN_HIT=0.50

KLINE_COLS=['open_time','open','high','low','close','volume','close_time','quote_volume','count','taker_buy_volume','taker_buy_quote_volume','ignore']
METRIC_COLS=['create_time','symbol','sum_open_interest','sum_open_interest_value','count_toptrader_long_short_ratio','sum_toptrader_long_short_ratio','count_long_short_ratio','sum_taker_long_short_vol_ratio']

def log(s): print(f"[{time.strftime('%H:%M:%S')}] {s}",flush=True)

def fetch_bytes(url):
    last=None
    for k in range(RETRIES):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'tst-round8-basis-premium/1.0'})
            with urllib.request.urlopen(req,timeout=25) as r:return r.read()
        except Exception as e:
            last=e; time.sleep(0.35*(2**k))
    raise RuntimeError(f'{url}: {last}')

def zip_csv(url, cols=None):
    raw=fetch_bytes(url)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:b=z.read(z.namelist()[0])
    txt=b.decode('utf-8-sig',errors='replace'); first=txt.splitlines()[0] if txt else ''
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
    d=START.date(); e=END.date(); out=[]
    while d<e:out.append(d.isoformat());d+=timedelta(days=1)
    return out

def _ts_from_open(series):
    t=pd.to_numeric(series,errors='coerce'); med=float(t.dropna().median())
    unit='us' if med>1e14 else 'ms'
    return pd.to_datetime(t,unit=unit,utc=True,errors='coerce')

def load_price(sym):
    frames=[zip_csv(f'{BASE}/monthly/klines/{sym}/5m/{sym}-5m-{ym}.zip',KLINE_COLS) for ym in months()]
    x=pd.concat(frames,ignore_index=True); x['ts']=_ts_from_open(x.open_time)
    for c in ['open','high','low','close']:x[c]=pd.to_numeric(x[c],errors='coerce')
    x=x.dropna(subset=['ts']).drop_duplicates('ts',keep='last').set_index('ts').sort_index()
    grid=pd.date_range(START,END-pd.Timedelta(minutes=5),freq='5min');x=x.reindex(grid)
    return x[['open','high','low','close']],float(x.close.notna().mean())

def load_premium(sym):
    frames=[]
    for ym in months():
        u=f'{BASE}/monthly/premiumIndexKlines/{sym}/5m/{sym}-5m-{ym}.zip'
        frames.append(zip_csv(u,KLINE_COLS))
    x=pd.concat(frames,ignore_index=True); x['ts']=_ts_from_open(x.open_time)
    x['premium']=pd.to_numeric(x.close,errors='coerce')
    x=x.dropna(subset=['ts']).drop_duplicates('ts',keep='last').set_index('ts').sort_index()
    grid=pd.date_range(START,END-pd.Timedelta(minutes=5),freq='5min');x=x.reindex(grid)
    return x[['premium']],float(x.premium.notna().mean())

def load_metrics(sym):
    urls=[f'{BASE}/daily/metrics/{sym}/{sym}-metrics-{d}.zip' for d in days()]
    frames=[];fails=[]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        fut={ex.submit(zip_csv,u,METRIC_COLS):u for u in urls}
        for f in as_completed(fut):
            try:frames.append(f.result())
            except Exception as e:fails.append(str(e))
    if not frames:raise RuntimeError(f'{sym}: no metrics')
    x=pd.concat(frames,ignore_index=True);x['create_time']=pd.to_datetime(x.create_time,utc=True,errors='coerce')
    for c in METRIC_COLS[2:]:x[c]=pd.to_numeric(x[c],errors='coerce')
    x=x.dropna(subset=['create_time']).sort_values('create_time')
    dup=int(x.duplicated('create_time').sum());x=x.drop_duplicates('create_time',keep='last').set_index('create_time')
    grid=pd.date_range(START,END-pd.Timedelta(minutes=5),freq='5min');x=x.reindex(grid)
    return x,{'coverage':float(x.sum_open_interest_value.notna().mean()),'duplicates':dup,'downloadFailures':len(fails)}

def decluster(ix):
    out=[];last=-10**18
    for i in sorted(ix):
        if i-last>=MIN_GAP:out.append(i);last=i
    return out

def pmean(a):
    if len(a)<2:return 1.0
    sd=float(a.std(ddof=1))
    if sd<=0:return 0.0 if float(a.mean())>0 else 1.0
    z=float(a.mean())/(sd/math.sqrt(len(a)));return 0.5*math.erfc(z/math.sqrt(2))

def stats_from_records(records,h,drop_symbol=None):
    vals=[];mfe=[];mae=[];symbols=[]
    for sym,df,i,sign in records:
        if sym==drop_symbol or i+h>=len(df):continue
        e=float(df.close.iloc[i]);f=float(df.close.iloc[i+h])
        if not np.isfinite(e) or not np.isfinite(f) or e<=0:continue
        r=(f/e-1)*sign;vals.append(r);symbols.append(sym)
        hi=float(df.high.iloc[i+1:i+h+1].max())/e-1;lo=1-float(df.low.iloc[i+1:i+h+1].min())/e
        if sign>0:mfe.append(hi);mae.append(max(0.0,lo))
        else:mfe.append(lo);mae.append(max(0.0,hi))
    if not vals:return None
    a=np.asarray(vals);m=np.asarray(mfe);d=np.asarray(mae);med_mae=float(np.median(d));ratio=float(np.median(m))/med_mae if med_mae>0 else float('inf')
    per={}
    for s in sorted(set(symbols)):
        z=np.asarray([v for v,ss in zip(vals,symbols) if ss==s]);per[s]={'n':len(z),'mean':float(z.mean()),'hitRate':float((z>0).mean())}
    return {'n':len(a),'mean':float(a.mean()),'median':float(np.median(a)),'hitRate':float((a>0).mean()),'medianMFE':float(np.median(m)),'medianMAE':med_mae,'mfeMaeRatio':ratio,'pMeanPositive':pmean(a),'perSymbol':per}

def robustness(st,records,h):
    eligible=[s for s,v in st['perSymbol'].items() if v['n']>=5]
    positive=sum(st['perSymbol'][s]['mean']>0 for s in eligible)
    max_share=max((v['n']/st['n'] for v in st['perSymbol'].values()),default=1.0)
    loo={};loo_ok=True
    for s in st['perSymbol']:
        q=stats_from_records(records,h,drop_symbol=s)
        if q:
            loo[s]={'n':q['n'],'mean':q['mean'],'hitRate':q['hitRate']}
            if q['mean']<=LOO_MIN_MEAN or q['hitRate']<=LOO_MIN_HIT:loo_ok=False
    robust=positive>=MIN_POSITIVE_SYMBOLS and max_share<=MAX_EVENT_SHARE and loo_ok
    return {'eligibleSymbols':eligible,'positiveSymbols':positive,'maxEventShare':max_share,'leaveOneOut':loo,'robustPass':robust}

def bh(rows):
    if not rows:return
    ps=sorted([(i,r['pMeanPositive']) for i,r in enumerate(rows)],key=lambda x:x[1]);m=len(ps);adj=[1.0]*m;run=1.0
    for rank in range(m,0,-1):
        i,p=ps[rank-1];run=min(run,p*m/rank);adj[i]=min(1.0,run)
    for i,r in enumerate(rows):r['qValue']=adj[i];r['pass']=bool(r['rawPass'] and r['robustness']['robustPass'] and adj[i]<=FDR_Q)

def main():
    t0=time.time();frames={};quality={};excluded={}
    for sym in SYMBOLS:
        log(f'LOAD {sym}')
        try:p,pc=load_price(sym);pr,prc=load_premium(sym);m,mq=load_metrics(sym)
        except Exception as e:excluded[sym]=str(e);continue
        quality[sym]={'priceCoverage':pc,'premiumCoverage':prc,'metrics':mq}
        if pc<PRICE_MIN_COVERAGE or prc<PREMIUM_MIN_COVERAGE or mq['coverage']<METRICS_MIN_COVERAGE:
            excluded[sym]='DATA_QUALITY_GATE';continue
        x=p.join(pr,how='left').join(m,how='left')
        roll=x.premium.rolling(PREM_Z_LOOKBACK,min_periods=PREM_Z_LOOKBACK)
        x['prem_z']=(x.premium-roll.mean())/roll.std(ddof=1)
        x['oi4h']=x.sum_open_interest_value.pct_change(48,fill_method=None)
        x['taker15']=x.sum_taker_long_short_vol_ratio.rolling(3,min_periods=3).mean()
        frames[sym]=x
        log(f'{sym} OK price={pc:.4f} premium={prc:.4f} metrics={mq["coverage"]:.4f}')
    pools={
      'PREMIUM_TS_REVERSION_V1:NEG_LONG':[],'PREMIUM_TS_REVERSION_V1:POS_SHORT':[],
      'PREMIUM_OI_SQUEEZE_V1:NEG_LONG':[],'PREMIUM_OI_SQUEEZE_V1:POS_SHORT':[],
      'CROSS_SECTIONAL_PREMIUM_EXTREME_V1:BOTTOM_LONG':[],'CROSS_SECTIONAL_PREMIUM_EXTREME_V1:TOP_SHORT':[]}
    for sym,x in frames.items():
        price=x[['open','high','low','close']]
        defs={
          'PREMIUM_TS_REVERSION_V1:NEG_LONG':((x.prem_z<=-PREM_Z_ABS)&(x.taker15<=PREM_TAKER_LONG_MAX),1),
          'PREMIUM_TS_REVERSION_V1:POS_SHORT':((x.prem_z>=PREM_Z_ABS)&(x.taker15>=PREM_TAKER_SHORT_MIN),-1),
          'PREMIUM_OI_SQUEEZE_V1:NEG_LONG':((x.prem_z<=-PREM_OI_Z_ABS)&(x.oi4h>=PREM_OI_4H_MIN),1),
          'PREMIUM_OI_SQUEEZE_V1:POS_SHORT':((x.prem_z>=PREM_OI_Z_ABS)&(x.oi4h>=PREM_OI_4H_MIN),-1)}
        for name,(sig,sign) in defs.items():
            for i in decluster(np.flatnonzero(sig.fillna(False).to_numpy()).tolist()):pools[name].append((sym,price,i,sign))
    # Cross-sectional rank family. Signal only when all loaded symbols have a contemporaneous premium z-score.
    if len(frames)>=5:
        zmat=pd.DataFrame({s:x.prem_z for s,x in frames.items()})
        ranks=zmat.rank(axis=1,pct=True,method='average')
        for sym,x in frames.items():
            price=x[['open','high','low','close']];valid=zmat.notna().sum(axis=1)>=5
            lo=(ranks[sym]<=CS_BOTTOM_Q)&valid;hi=(ranks[sym]>=CS_TOP_Q)&valid
            for i in decluster(np.flatnonzero(lo.fillna(False).to_numpy()).tolist()):pools['CROSS_SECTIONAL_PREMIUM_EXTREME_V1:BOTTOM_LONG'].append((sym,price,i,1))
            for i in decluster(np.flatnonzero(hi.fillna(False).to_numpy()).tolist()):pools['CROSS_SECTIONAL_PREMIUM_EXTREME_V1:TOP_SHORT'].append((sym,price,i,-1))
    tests=[]
    for fam,recs in pools.items():
        log(f'{fam} events={len(recs)}')
        for label,h in HORIZONS.items():
            st=stats_from_records(recs,h)
            if not st:continue
            rb=robustness(st,recs,h)
            raw=bool(st['n']>=MIN_EVENTS and st['mean']>=MIN_MEAN and st['median']>0 and st['hitRate']>MIN_HIT and st['mfeMaeRatio']>=MIN_MFE_MAE)
            tests.append({'family':fam,'horizon':label,'bars':h,**st,'rawPass':raw,'robustness':rb})
    bh(tests);surv=sorted([r for r in tests if r['pass']],key=lambda r:(r['qValue'],-r['mean']))
    report={'engine':'BASIS_PREMIUM_EDGE_SCANNER_V1','round':8,'authorization':'RESEARCH_ONLY','liveTrading':False,'liveReady':False,
      'period':{'start':str(START),'endExclusive':str(END)},'symbols':SYMBOLS,'symbolsExcluded':excluded,'dataQuality':quality,
      'families':['PREMIUM_TS_REVERSION_V1','PREMIUM_OI_SQUEEZE_V1','CROSS_SECTIONAL_PREMIUM_EXTREME_V1'],
      'frozenDefinitions':{'PREMIUM_TS_REVERSION_V1':{'zLookbackBars5m':PREM_Z_LOOKBACK,'absZ':PREM_Z_ABS,'takerLongMax':PREM_TAKER_LONG_MAX,'takerShortMin':PREM_TAKER_SHORT_MIN},
       'PREMIUM_OI_SQUEEZE_V1':{'absZ':PREM_OI_Z_ABS,'oiRise4hMin':PREM_OI_4H_MIN},
       'CROSS_SECTIONAL_PREMIUM_EXTREME_V1':{'bottomPctRankLTE':CS_BOTTOM_Q,'topPctRankGTE':CS_TOP_Q,'minContemporaneousSymbols':5}},
      'rawGate':{'minimumEvents':MIN_EVENTS,'meanMin':MIN_MEAN,'medianPositive':True,'hitRateMinExclusive':MIN_HIT,'mfeMaeMin':MIN_MFE_MAE},
      'robustnessGate':{'positiveSymbolsMin':MIN_POSITIVE_SYMBOLS,'perSymbolMinEventsForPositiveCount':5,'maxEventShare':MAX_EVENT_SHARE,'leaveOneOutMeanMinExclusive':LOO_MIN_MEAN,'leaveOneOutHitRateMinExclusive':LOO_MIN_HIT},
      'multipleTesting':{'method':'Benjamini-Hochberg','q':FDR_Q},'declusterBars5m':MIN_GAP,'tests':tests,'survivors':surv,'survivorCount':len(surv),
      'decision':'SURVIVOR_NEEDS_FROZEN_OOS' if surv else 'NO_EDGE_FOUND','elapsedSec':time.time()-t0}
    OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(report,indent=2,sort_keys=True),encoding='utf-8');print(json.dumps(report,indent=2,sort_keys=True))

if __name__=='__main__':main()
