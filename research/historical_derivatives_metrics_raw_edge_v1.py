#!/usr/bin/env python3
"""Research-only raw-edge scan for Binance Vision USD-M `metrics` archives.

New feature family, independently split into Discovery/OOS. No TP/SL, no
position sizing, no live trading, and no post-hoc filters from prior flow tests.
"""
from __future__ import annotations

import argparse, hashlib, io, json, math, pathlib, urllib.request, zipfile
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from research.binance_vision_historical_feature_layer import build

AUTHORIZATION='RESEARCH_ONLY'
BASE='https://data.binance.vision/data/futures/um/daily/metrics'
UA='tst-historical-derivatives-metrics-raw-edge/1.1'
ZSCORE_WINDOW=288
ZSCORE_MIN=144
Z_TAIL=2.0
HORIZONS={'15m':3,'60m':12,'240m':48}
MIN_DISCOVERY_EVENTS=20
MIN_OOS_EVENTS=15
FEATURES=[
 'oi_contracts_chg_1h',
 'oi_value_chg_1h',
 'toptrader_accounts_log',
 'toptrader_positions_log',
 'global_long_short_log',
 'taker_long_short_log',
]


def days(start:date,end:date):
 d=start
 while d<=end:
  yield d; d+=timedelta(days=1)


def get(url:str)->bytes:
 req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'*/*'})
 with urllib.request.urlopen(req,timeout=60) as r: return r.read()


def load_metrics(symbol:str,start:date,end:date)->pd.DataFrame:
 parts=[]
 for d in days(start,end):
  stamp=d.isoformat(); url=f'{BASE}/{symbol}/{symbol}-metrics-{stamp}.zip'
  raw=get(url); ck=get(url+'.CHECKSUM').decode().strip().split()[0].lower()
  if hashlib.sha256(raw).hexdigest()!=ck: raise RuntimeError(f'checksum mismatch {url}')
  with zipfile.ZipFile(io.BytesIO(raw)) as z:
   csvs=[n for n in z.namelist() if n.lower().endswith('.csv')]
   if len(csvs)!=1: raise RuntimeError(f'bad metrics archive {url}: {csvs}')
   with z.open(csvs[0]) as f: parts.append(pd.read_csv(f))
 x=pd.concat(parts,ignore_index=True)
 required=['create_time','symbol','sum_open_interest','sum_open_interest_value','count_toptrader_long_short_ratio','sum_toptrader_long_short_ratio','count_long_short_ratio','sum_taker_long_short_vol_ratio']
 miss=[c for c in required if c not in x.columns]
 if miss: raise RuntimeError(f'missing metrics columns {miss}')
 x=x[required].copy(); x['ts']=pd.to_datetime(x['create_time'],utc=True,errors='coerce')
 # Pandas 3 may preserve microsecond datetime resolution from strings while
 # Binance trade archives normalize to millisecond/nanosecond resolution.
 # Force a common ns UTC dtype before merge_asof so timestamp resolution is
 # never a hidden source of failure or silent mismatch.
 x['ts']=pd.DatetimeIndex(x['ts']).as_unit('ns')
 for c in required[2:]: x[c]=pd.to_numeric(x[c],errors='coerce')
 return x.dropna(subset=['ts']).sort_values('ts')


def rolling_z(s):
 m=s.rolling(ZSCORE_WINDOW,min_periods=ZSCORE_MIN).mean(); sd=s.rolling(ZSCORE_WINDOW,min_periods=ZSCORE_MIN).std(ddof=0).replace(0,np.nan)
 return (s-m)/sd


def prepare(metrics_df,price_df):
 m=metrics_df.copy()
 m['oi_contracts_chg_1h']=m['sum_open_interest'].pct_change(12)
 m['oi_value_chg_1h']=m['sum_open_interest_value'].pct_change(12)
 for src,dst in [
  ('count_toptrader_long_short_ratio','toptrader_accounts_log'),
  ('sum_toptrader_long_short_ratio','toptrader_positions_log'),
  ('count_long_short_ratio','global_long_short_log'),
  ('sum_taker_long_short_vol_ratio','taker_long_short_log')]:
  m[dst]=np.log(pd.to_numeric(m[src],errors='coerce').where(lambda s:s>0))
 for f in FEATURES: m[f+'__z']=rolling_z(m[f].astype(float))
 p=price_df[['ts','trade_close']].copy(); p['ts']=pd.DatetimeIndex(pd.to_datetime(p['ts'],utc=True)).as_unit('ns')
 m['ts']=pd.DatetimeIndex(pd.to_datetime(m['ts'],utc=True)).as_unit('ns')
 x=pd.merge_asof(m.sort_values('ts'),p.sort_values('ts'),on='ts',direction='nearest',tolerance=pd.Timedelta('2min'))
 for h,b in HORIZONS.items(): x['fwd_'+h]=x['trade_close'].shift(-b)/x['trade_close']-1.0
 return x


def met(s):
 v=pd.to_numeric(s,errors='coerce').dropna().astype(float)
 if len(v)==0:return {'n':0}
 pos=float(v[v>0].sum()); neg=float(-v[v<0].sum())
 return {'n':int(len(v)),'mean':float(v.mean()),'median':float(v.median()),'hitRate':float((v>0).mean()),'profitFactor':float(pos/neg if neg>0 else (999.0 if pos>0 else 0.0))}


def gate(x,min_n):
 return bool(x.get('n',0)>=min_n and x.get('mean',-1)>0 and x.get('median',-1)>0 and x.get('hitRate',0)>0.55 and x.get('profitFactor',0)>=1.20)


def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--symbol',required=True); ap.add_argument('--start',default='2025-01-01'); ap.add_argument('--split',default='2025-01-15'); ap.add_argument('--end',default='2025-01-28'); ap.add_argument('--output-dir',default='artifacts/historical-derivatives-metrics-edge'); a=ap.parse_args()
 s=a.symbol.upper(); start=datetime.strptime(a.start,'%Y-%m-%d').date(); end=datetime.strptime(a.end,'%Y-%m-%d').date(); split=pd.Timestamp(a.split,tz='UTC')
 md=load_metrics(s,start,end); pdx,meta=build(s,start,end,'5min',True)
 if meta['failures']: raise RuntimeError(meta['failures'][:5])
 x=prepare(md,pdx); d=x[x.ts<split]; o=x[x.ts>=split]
 tests=[]
 for f in FEATURES:
  for side in ('HIGH','LOW'):
   dm=d[f+'__z']>=Z_TAIL if side=='HIGH' else d[f+'__z']<=-Z_TAIL
   om=o[f+'__z']>=Z_TAIL if side=='HIGH' else o[f+'__z']<=-Z_TAIL
   for h in HORIZONS:
    dmet=met(d.loc[dm,'fwd_'+h]); omet=met(o.loc[om,'fwd_'+h]); dp=gate(dmet,MIN_DISCOVERY_EVENTS); op=gate(omet,MIN_OOS_EVENTS)
    tests.append({'feature':f,'side':side,'threshold':f"z {'>=' if side=='HIGH' else '<='} {2.0 if side=='HIGH' else -2.0}",'horizon':h,'discovery':{**dmet,'pass':dp},'oos':{**omet,'pass':op},'survivor':bool(dp and op)})
 surv=[t for t in tests if t['survivor']]
 r={'engine':'HISTORICAL_DERIVATIVES_METRICS_RAW_EDGE_V1','authorization':AUTHORIZATION,'liveTrading':False,'automaticPromotion':False,'symbol':s,'window':{'start':a.start,'split':a.split,'end':a.end},'frozenDefinition':{'frequency':'5min','zWindowBars':ZSCORE_WINDOW,'zTail':Z_TAIL,'features':FEATURES,'horizons':HORIZONS,'noTpSl':True,'noSizing':True},'rows':len(x),'tests':tests,'survivors':surv,'survivorCount':len(surv),'verdict':'SURVIVOR_FOUND' if surv else 'NO_STABLE_RAW_EDGE'}
 out=pathlib.Path(a.output_dir); out.mkdir(parents=True,exist_ok=True); p=out/f'{s}-derivatives-metrics-edge-v1.json'; p.write_text(json.dumps(r,indent=2,sort_keys=True)); print(json.dumps({'symbol':s,'rows':len(x),'survivors':len(surv),'survivorDefs':[(z['feature'],z['side'],z['horizon']) for z in surv]},separators=(',',':')))
if __name__=='__main__': main()
