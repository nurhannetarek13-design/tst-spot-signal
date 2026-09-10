#!/usr/bin/env python3
from __future__ import annotations
import io, json, math, pathlib, urllib.request, zipfile, os
from datetime import date, timedelta
import numpy as np
import pandas as pd
import research.cryptolake_l2_round as cl

AUTHORIZATION='RESEARCH_ONLY'
CANDIDATE='H2_MULTI_DEPTH_CONTINUATION'
HORIZON=240
START=date(2025,1,9); END=date(2025,2,12)
DISC_END=pd.Timestamp('2025-01-16',tz='UTC')
OOS_END=pd.Timestamp('2025-01-30',tz='UTC')
UNT_END=pd.Timestamp('2025-02-13',tz='UTC')
MIN_SNAPSHOTS_PER_5M=100
NORMAL_COST=.0028; STRESS_COST=.0050


def load_spot_month(month):
    sym='SOLUSDT'; url=f'https://data.binance.vision/data/spot/monthly/klines/{sym}/1m/{sym}-1m-{month}.zip'
    raw=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'tst-h2-frozen/1.0'}),timeout=180).read()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        n=[x for x in z.namelist() if x.endswith('.csv')][0]
        a=pd.read_csv(z.open(n),header=None,usecols=[0,1],names=['open_ts','open'])
    a.open_ts=pd.to_numeric(a.open_ts,errors='coerce'); med=float(a.open_ts.dropna().median()); unit='us' if med>1e14 else 'ms'
    a['ts']=pd.to_datetime(a.open_ts,unit=unit,utc=True,errors='coerce'); a['open']=pd.to_numeric(a.open,errors='coerce')
    return a.dropna().sort_values('ts')[['ts','open']]

def prepare_spot_targets(x):
    spot=pd.concat([load_spot_month('2025-01'),load_spot_month('2025-02')],ignore_index=True).drop_duplicates('ts').sort_values('ts')
    px=spot.set_index('ts').open
    x=x.copy(); x['entry_ts']=x.ts+pd.Timedelta(minutes=1); x['exit_ts']=x.entry_ts+pd.Timedelta(minutes=HORIZON)
    x['entry']=x.entry_ts.map(px); x['exit']=x.exit_ts.map(px); x['fwd']=x['exit']/x['entry']-1
    return x

def met(v):
    v=pd.Series(v).dropna().astype(float)
    if len(v)==0:return {'n':0,'mean':None,'median':None,'hitRate':None,'profitFactor':None,'pOneSided':None}
    pos=v[v>0].sum(); neg=-v[v<0].sum(); pf=float(pos/neg) if neg>0 else 999.
    if len(v)>1 and v.std(ddof=1)>0:
        z=float(v.mean()/(v.std(ddof=1)/math.sqrt(len(v)))); p=.5*math.erfc(z/math.sqrt(2))
    else:p=1.
    return {'n':int(len(v)),'mean':float(v.mean()),'median':float(v.median()),'hitRate':float((v>0).mean()),'profitFactor':pf,'pOneSided':float(p)}
def decluster(e):
    keep=[]; last=None; gap=pd.Timedelta(minutes=HORIZON)
    for i,r in e.sort_values('ts').iterrows():
        if last is None or r.ts-last>=gap:keep.append(i); last=r.ts
    return e.loc[keep]
def gate(m,n=15):
    return m['n']>=n and m['mean'] is not None and m['mean']>0 and m['median']>0 and m['hitRate']>.55 and m['profitFactor']>=1.2 and m['pOneSided']<=.10

def main():
    root=pathlib.Path('/tmp/cryptolake-h2'); frames=[]; daily=[]; d=START
    while d<=END:
        ds=d.isoformat(); rel=f'raw/book/exchange=BINANCE_FUTURES/symbol={cl.SYMBOL}/dt={ds}/1.snappy.parquet'
        p=cl.dl(rel,root/f'{ds}.parquet'); b=cl.process_book(p); os.remove(p)
        daily.append({'date':ds,'rows5m':int(len(b)),'medianSnapshotsPer5m':float(b.snapshot_count.median()),'lowCoverageBars':int((b.snapshot_count<MIN_SNAPSHOTS_PER_5M).sum())})
        frames.append(b); d+=timedelta(days=1)
    x=pd.concat(frames,ignore_index=True).sort_values('ts').drop_duplicates('ts').reset_index(drop=True)
    # Quality gate fixed before seeing validation returns.
    x=x[x.snapshot_count>=MIN_SNAPSHOTS_PER_5M].copy().reset_index(drop=True)
    x['snapshot_ofi_z']=cl.rz(x.snapshot_ofi.astype(float)); x['micro_dev_bps_z']=cl.rz(x.micro_dev_bps.astype(float))
    x=prepare_spot_targets(x)
    event=(x.snapshot_ofi_z>=2.5)&(x.imb_1>0)&(x.imb_5>0)&(x.imb_10>0)&(x.micro_dev_bps>0)
    x['event']=event
    windows={
      'discovery':(pd.Timestamp('2025-01-09',tz='UTC'),DISC_END),
      'oos':(DISC_END,OOS_END),
      'untouched':(OOS_END,UNT_END),
    }
    results={}
    for name,(lo,hi) in windows.items():
        e=x[(x.ts>=lo)&(x.ts<hi)&x.event][['ts','fwd']].dropna(); e=decluster(e)
        gross=met(e.fwd); normal=met(e.fwd-NORMAL_COST); stress=met(e.fwd-STRESS_COST)
        results[name]={'declusteredEvents':int(len(e)),'gross':gross,'normalNet':normal,'stressNet':stress,'normalGatePass':gate(normal),'stressMeanPositive':bool(stress['mean'] is not None and stress['mean']>0)}
    prod=bool(results['oos']['normalGatePass'] and results['oos']['stressMeanPositive'] and results['untouched']['normalGatePass'] and results['untouched']['stressMeanPositive'])
    out={'engine':'CRYPTOLAKE_H2_FROZEN_VALIDATE_V1','authorization':AUTHORIZATION,'liveTrading':False,'automaticPromotion':False,
         'candidate':CANDIDATE,'horizonMin':HORIZON,'featureMarket':'BINANCE_FUTURES_L2','targetMarket':'BINANCE_SPOT','entryRule':'next 1m Spot open after 5m event bar','eventDefinition':'snapshot_ofi_z>=2.5 AND imb_1>0 AND imb_5>0 AND imb_10>0 AND micro_dev_bps>0','qualityGate':{'minSnapshotsPer5m':MIN_SNAPSHOTS_PER_5M},
         'costs':{'normalRoundTrip':NORMAL_COST,'stressRoundTrip':STRESS_COST},'dailyQuality':daily,'results':results,'productionCandidate':prod,'verdict':'PRODUCTION_EDGE_FOUND' if prod else 'REJECT_FROZEN_H2'}
    od=pathlib.Path('artifacts/cryptolake-h2-frozen'); od.mkdir(parents=True,exist_ok=True); (od/'verdict.json').write_text(json.dumps(out,indent=2)); print(json.dumps(out,indent=2))
if __name__=='__main__':main()
