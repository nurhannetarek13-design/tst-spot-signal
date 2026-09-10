#!/usr/bin/env python3
"""Research-only derivatives-metrics raw-edge scan against Binance Spot returns.

Uses Binance Vision USD-M metrics as explanatory features and Binance Spot 5m
klines as the target market. Discovery/OOS split is fixed before evaluation.
No TP/SL, sizing, live trading, or post-hoc tuning.
"""
from __future__ import annotations

import argparse, hashlib, io, json, pathlib, urllib.request, zipfile
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

AUTHORIZATION='RESEARCH_ONLY'
METRICS_BASE='https://data.binance.vision/data/futures/um/daily/metrics'
SPOT_BASE='https://data.binance.vision/data/spot/daily/klines'
UA='tst-historical-derivatives-metrics-raw-edge/2.0'
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
KLINE_COLS=['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_buy_base','taker_buy_quote','ignore']


def days(start:date,end:date):
 d=start
 while d<=end:
  yield d
  d+=timedelta(days=1)


def get(url:str)->bytes:
 req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'*/*'})
 with urllib.request.urlopen(req,timeout=60) as r:
  return r.read()


def get_verified_zip(url:str)->bytes:
 raw=get(url)
 expected=get(url+'.CHECKSUM').decode('utf-8',errors='replace').strip().split()[0].lower()
 actual=hashlib.sha256(raw).hexdigest()
 if expected!=actual:
  raise RuntimeError(f'checksum mismatch {url}: expected={expected} actual={actual}')
 return raw


def normalize_ts_numeric(s:pd.Series)->pd.Series:
 x=pd.to_numeric(s,errors='coerce')
 med=float(x.dropna().median()) if x.notna().any() else 0.0
 unit='ns' if med>1e17 else ('us' if med>1e14 else 'ms')
 return pd.to_datetime(x,unit=unit,utc=True,errors='coerce').astype('datetime64[ns, UTC]')


def load_metrics(symbol:str,start:date,end:date)->pd.DataFrame:
 parts=[]
 for d in days(start,end):
  stamp=d.isoformat(); url=f'{METRICS_BASE}/{symbol}/{symbol}-metrics-{stamp}.zip'
  raw=get_verified_zip(url)
  with zipfile.ZipFile(io.BytesIO(raw)) as z:
   csvs=[n for n in z.namelist() if n.lower().endswith('.csv')]
   if len(csvs)!=1: raise RuntimeError(f'bad metrics archive {url}: {csvs}')
   with z.open(csvs[0]) as f: parts.append(pd.read_csv(f))
 x=pd.concat(parts,ignore_index=True)
 required=['create_time','symbol','sum_open_interest','sum_open_interest_value','count_toptrader_long_short_ratio','sum_toptrader_long_short_ratio','count_long_short_ratio','sum_taker_long_short_vol_ratio']
 miss=[c for c in required if c not in x.columns]
 if miss: raise RuntimeError(f'missing metrics columns {miss}')
 x=x[required].copy()
 x['ts']=pd.to_datetime(x['create_time'],utc=True,errors='coerce').astype('datetime64[ns, UTC]')
 for c in required[2:]: x[c]=pd.to_numeric(x[c],errors='coerce')
 return x.dropna(subset=['ts']).sort_values('ts').drop_duplicates('ts',keep='last')


def load_spot_5m(symbol:str,start:date,end:date)->pd.DataFrame:
 parts=[]
 for d in days(start,end):
  stamp=d.isoformat(); url=f'{SPOT_BASE}/{symbol}/5m/{symbol}-5m-{stamp}.zip'
  raw=get_verified_zip(url)
  with zipfile.ZipFile(io.BytesIO(raw)) as z:
   csvs=[n for n in z.namelist() if n.lower().endswith('.csv')]
   if len(csvs)!=1: raise RuntimeError(f'bad spot kline archive {url}: {csvs}')
   with z.open(csvs[0]) as f:
    probe=pd.read_csv(f,nrows=2,header=None)
   first=str(probe.iloc[0,0]).strip().lower() if len(probe) else ''
   has_header=not first.replace('-','').isdigit()
   with z.open(csvs[0]) as f:
    df=pd.read_csv(f,header=0 if has_header else None)
   if df.shape[1] < len(KLINE_COLS): raise RuntimeError(f'bad spot kline column count {df.shape[1]}')
   df=df.iloc[:,:len(KLINE_COLS)].copy(); df.columns=KLINE_COLS
   parts.append(df)
 x=pd.concat(parts,ignore_index=True)
 x['ts']=normalize_ts_numeric(x['open_time'])
 x['spot_close']=pd.to_numeric(x['close'],errors='coerce')
 return x[['ts','spot_close']].dropna().sort_values('ts').drop_duplicates('ts',keep='last')


def rolling_z(s):
 m=s.rolling(ZSCORE_WINDOW,min_periods=ZSCORE_MIN).mean()
 sd=s.rolling(ZSCORE_WINDOW,min_periods=ZSCORE_MIN).std(ddof=0).replace(0,np.nan)
 return (s-m)/sd


def prepare(metrics_df,spot_df):
 m=metrics_df.copy()
 m['oi_contracts_chg_1h']=m['sum_open_interest'].pct_change(12)
 m['oi_value_chg_1h']=m['sum_open_interest_value'].pct_change(12)
 for src,dst in [
  ('count_toptrader_long_short_ratio','toptrader_accounts_log'),
  ('sum_toptrader_long_short_ratio','toptrader_positions_log'),
  ('count_long_short_ratio','global_long_short_log'),
  ('sum_taker_long_short_vol_ratio','taker_long_short_log')]:
  m[dst]=np.log(pd.to_numeric(m[src],errors='coerce').where(lambda s:s>0))
 for f in FEATURES:
  m[f+'__z']=rolling_z(m[f].astype(float))
 m['ts']=pd.to_datetime(m['ts'],utc=True).astype('datetime64[ns, UTC]')
 p=spot_df.copy(); p['ts']=pd.to_datetime(p['ts'],utc=True).astype('datetime64[ns, UTC]')
 x=pd.merge_asof(m.sort_values('ts'),p.sort_values('ts'),on='ts',direction='backward',tolerance=pd.Timedelta('5min'))
 for h,b in HORIZONS.items():
  x['fwd_'+h]=x['spot_close'].shift(-b)/x['spot_close']-1.0
 return x


def met(s):
 v=pd.to_numeric(s,errors='coerce').dropna().astype(float)
 if len(v)==0: return {'n':0}
 pos=float(v[v>0].sum()); neg=float(-v[v<0].sum())
 return {'n':int(len(v)),'mean':float(v.mean()),'median':float(v.median()),'hitRate':float((v>0).mean()),'profitFactor':float(pos/neg if neg>0 else (999.0 if pos>0 else 0.0))}


def gate(x,min_n):
 return bool(x.get('n',0)>=min_n and x.get('mean',-1)>0 and x.get('median',-1)>0 and x.get('hitRate',0)>0.55 and x.get('profitFactor',0)>=1.20)


def main():
 ap=argparse.ArgumentParser()
 ap.add_argument('--symbol',required=True)
 ap.add_argument('--start',default='2025-01-01')
 ap.add_argument('--split',default='2025-01-15')
 ap.add_argument('--end',default='2025-01-28')
 ap.add_argument('--output-dir',default='artifacts/historical-derivatives-metrics-edge')
 a=ap.parse_args()
 s=a.symbol.upper(); start=datetime.strptime(a.start,'%Y-%m-%d').date(); end=datetime.strptime(a.end,'%Y-%m-%d').date(); split=pd.Timestamp(a.split,tz='UTC')
 md=load_metrics(s,start,end); px=load_spot_5m(s,start,end); x=prepare(md,px)
 if x['spot_close'].notna().sum()==0: raise RuntimeError('no aligned Spot prices')
 d=x[x.ts<split]; o=x[x.ts>=split]
 tests=[]
 for f in FEATURES:
  for side in ('HIGH','LOW'):
   dm=d[f+'__z']>=Z_TAIL if side=='HIGH' else d[f+'__z']<=-Z_TAIL
   om=o[f+'__z']>=Z_TAIL if side=='HIGH' else o[f+'__z']<=-Z_TAIL
   for h in HORIZONS:
    dmet=met(d.loc[dm,'fwd_'+h]); omet=met(o.loc[om,'fwd_'+h]); dp=gate(dmet,MIN_DISCOVERY_EVENTS); op=gate(omet,MIN_OOS_EVENTS)
    tests.append({'feature':f,'side':side,'threshold':f"z {'>=' if side=='HIGH' else '<='} {Z_TAIL if side=='HIGH' else -Z_TAIL}",'horizon':h,'discovery':{**dmet,'pass':dp},'oos':{**omet,'pass':op},'survivor':bool(dp and op)})
 surv=[t for t in tests if t['survivor']]
 r={'engine':'HISTORICAL_DERIVATIVES_METRICS_SPOT_RAW_EDGE_V2','authorization':AUTHORIZATION,'liveTrading':False,'automaticPromotion':False,'symbol':s,'targetMarket':'BINANCE_SPOT','featureMarket':'BINANCE_USDM_FUTURES','window':{'start':a.start,'split':a.split,'end':a.end},'frozenDefinition':{'frequency':'5min','zWindowBars':ZSCORE_WINDOW,'zTail':Z_TAIL,'features':FEATURES,'horizons':HORIZONS,'priceAlignment':'backward <=5m','noTpSl':True,'noSizing':True},'rows':len(x),'alignedSpotRows':int(x['spot_close'].notna().sum()),'tests':tests,'survivors':surv,'survivorCount':len(surv),'verdict':'SURVIVOR_FOUND' if surv else 'NO_STABLE_RAW_EDGE'}
 out=pathlib.Path(a.output_dir); out.mkdir(parents=True,exist_ok=True); p=out/f'{s}-derivatives-metrics-spot-edge-v2.json'; p.write_text(json.dumps(r,indent=2,sort_keys=True)); print(json.dumps({'symbol':s,'rows':len(x),'alignedSpotRows':r['alignedSpotRows'],'survivors':len(surv),'survivorDefs':[(z['feature'],z['side'],z['horizon']) for z in surv]},separators=(',',':')))

if __name__=='__main__': main()
