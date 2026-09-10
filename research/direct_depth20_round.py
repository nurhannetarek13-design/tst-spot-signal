#!/usr/bin/env python3
"""Direct top-20 Binance futures order-book research using public Parquet states.
No full-book reconstruction required. Research-only; no live trading or promotion.
"""
from __future__ import annotations
import argparse, io, json, math, pathlib, re, urllib.request, zipfile
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from huggingface_hub import HfApi, hf_hub_download

REPO='predict-quant/binance-future-orderbook'
AUTHORIZATION='RESEARCH_ONLY'
NORMAL_COST=0.0028; STRESS_COST=0.0050
HORIZONS=(15,60,240)
MIN_FILE=5_000_000
MAX_DAYS=15
HYPOTHESES=[
 'H1_SELL_EXHAUST_RECOVERY',
 'H2_MLOFI_CONTINUATION',
 'H3_MICROPRICE_CONFIRM',
 'H4_ABSORPTION_REVERSAL',
 'H5_ASK_DEPLETION',
]

def rz(s,w=288,minp=144):
 m=s.rolling(w,min_periods=minp).mean(); sd=s.rolling(w,min_periods=minp).std(ddof=0).replace(0,np.nan); return (s-m)/sd

def met(v):
 v=pd.Series(v).dropna().astype(float)
 if len(v)==0:return {'n':0,'mean':None,'median':None,'hitRate':None,'profitFactor':None,'pOneSided':None}
 pos=v[v>0].sum(); neg=-v[v<0].sum(); pf=float(pos/neg) if neg>0 else 999.0
 if len(v)>1 and v.std(ddof=1)>0:
  z=float(v.mean()/(v.std(ddof=1)/math.sqrt(len(v)))); p=.5*math.erfc(z/math.sqrt(2))
 else:p=1.0
 return {'n':int(len(v)),'mean':float(v.mean()),'median':float(v.median()),'hitRate':float((v>0).mean()),'profitFactor':pf,'pOneSided':float(p)}

def gate(m,n):return m['n']>=n and m['mean'] is not None and m['mean']>0 and m['median']>0 and m['hitRate']>.55 and m['profitFactor']>=1.2

def bh(ps):
 n=len(ps)
 if not n:return []
 order=sorted(range(n),key=lambda i:ps[i]); q=[1.]*n; prev=1.
 for j in range(n-1,-1,-1):
  i=order[j]; prev=min(prev,ps[i]*n/(j+1)); q[i]=prev
 return q

def decluster(e,h):
 keep=[]; last=None; gap=pd.Timedelta(minutes=h)
 for i,r in e.sort_values('ts').iterrows():
  if last is None or r.ts-last>=gap:keep.append(i); last=r.ts
 return e.loc[keep]

def select_files(symbol):
 api=HfApi(); items=[]
 for it in api.list_repo_tree(REPO,repo_type='dataset',recursive=True,expand=True):
  p=getattr(it,'path',None); sz=getattr(it,'size',None)
  if p and p.startswith(symbol+'/') and p.endswith('_depth20.parquet') and isinstance(sz,int) and sz>=MIN_FILE:
   m=re.search(r'(\d{4}-\d{2}-\d{2})',p)
   if m:items.append((m.group(1),p,sz))
 items=sorted(items)[:MAX_DAYS]
 return items

def parse_book(s):
 try:return [(float(p),float(q)) for p,q in json.loads(s)]
 except Exception:return []

def event_features(path):
 pf=pq.ParquetFile(path); outs=[]; prev=None; prev_u=None; resets=0; gaps=0
 for batch in pf.iter_batches(batch_size=50000,columns=['e','E','T','U','u','pu','bids','asks']):
  d=batch.to_pydict()
  for typ,E,T,U,u,pu,bs,ass in zip(d['e'],d['E'],d['T'],d['U'],d['u'],d['pu'],d['bids'],d['asks']):
   bids=parse_book(bs); asks=parse_book(ass)
   if not bids or not asks: continue
   bids=sorted(bids,key=lambda z:z[0],reverse=True)[:20]; asks=sorted(asks,key=lambda z:z[0])[:20]
   if bids[0][0]>=asks[0][0]:continue
   if typ=='snapshot': prev=None; prev_u=None; resets+=1
   elif prev_u is not None and pu is not None and int(pu)!=int(prev_u): prev=None; gaps+=1
   bp,bv=bids[0]; ap,av=asks[0]; mid=(bp+ap)/2; den=bv+av
   mp=(ap*bv+bp*av)/den if den else mid
   row={'ts':pd.to_datetime(int(T or E),unit='ms',utc=True),'mid':mid,'spread_bps':(ap-bp)/mid*10000,'micro_dev_bps':(mp/mid-1)*10000,'ofi':0.0}
   for n in (1,5,10,20):
    bd=sum(q for _,q in bids[:n]); ad=sum(q for _,q in asks[:n]); td=bd+ad
    row[f'bid_depth_{n}']=bd; row[f'ask_depth_{n}']=ad; row[f'imb_{n}']=(bd-ad)/td if td else np.nan
   cur=(bp,bv,ap,av)
   if prev is not None:
    pb,pbv,pa,pav=prev; e=0.0
    if bp>=pb:e+=bv
    if bp<=pb:e-=pbv
    if ap<=pa:e-=av
    if ap>=pa:e+=pav
    row['ofi']=e
   prev=cur
   if u is not None:prev_u=int(u)
   outs.append(row)
 df=pd.DataFrame(outs)
 if df.empty:return df,{'rows':0,'snapshotResets':resets,'puGaps':gaps}
 df=df.sort_values('ts').set_index('ts')
 agg={c:'last' for c in df.columns if c!='ofi'}; agg['ofi']='sum'
 x=df.resample('5min',label='right',closed='right').agg(agg).dropna(subset=['mid']).reset_index()
 return x,{'rows':len(outs),'bars5m':len(x),'snapshotResets':resets,'puGaps':gaps}

def load_monthly(symbol,month,market='spot'):
 if market=='spot':
  url=f'https://data.binance.vision/data/spot/monthly/klines/{symbol}/1m/{symbol}-1m-{month}.zip'
  raw=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'depth20-research'}),timeout=180).read()
  with zipfile.ZipFile(io.BytesIO(raw)) as z:
   n=[n for n in z.namelist() if n.endswith('.csv')][0]
   a=pd.read_csv(z.open(n),header=None,usecols=[0,1],names=['t','open'])
  a.t=pd.to_numeric(a.t,errors='coerce'); unit='us' if a.t.dropna().median()>1e14 else 'ms'; a['ts']=pd.to_datetime(a.t,unit=unit,utc=True,errors='coerce')
  a.open=pd.to_numeric(a.open,errors='coerce'); return a.dropna()[['ts','open']].sort_values('ts')
 url=f'https://data.binance.vision/data/futures/um/monthly/klines/{symbol}/5m/{symbol}-5m-{month}.zip'
 raw=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'depth20-research'}),timeout=180).read()
 with zipfile.ZipFile(io.BytesIO(raw)) as z:
  n=[n for n in z.namelist() if n.endswith('.csv')][0]
  a=pd.read_csv(z.open(n),header=None,usecols=[0,7,10],names=['t','quote','taker_buy_quote'])
 a.t=pd.to_numeric(a.t,errors='coerce'); a['ts']=pd.to_datetime(a.t,unit='ms',utc=True,errors='coerce'); a.quote=pd.to_numeric(a.quote,errors='coerce'); a.taker_buy_quote=pd.to_numeric(a.taker_buy_quote,errors='coerce')
 a['sell_quote']=(a.quote-a.taker_buy_quote).clip(lower=0); a['buy_share']=a.taker_buy_quote/a.quote.replace(0,np.nan)
 return a.dropna(subset=['ts'])[['ts','quote','taker_buy_quote','sell_quote','buy_share']].sort_values('ts')

def enrich(x,symbol,months):
 spot=pd.concat([load_monthly(symbol,m,'spot') for m in months]).drop_duplicates('ts').sort_values('ts')
 fut=pd.concat([load_monthly(symbol,m,'fut') for m in months]).drop_duplicates('ts').sort_values('ts')
 x=x.sort_values('ts'); x=pd.merge_asof(x,fut,on='ts',direction='backward',tolerance=pd.Timedelta('5min'))
 right=spot.copy(); right['entry_ts']=(right.ts-pd.Timedelta(minutes=1)).astype('datetime64[ns, UTC]')
 x=pd.merge_asof(x,right[['entry_ts','open']].rename(columns={'open':'entry'}).sort_values('entry_ts'),left_on='ts',right_on='entry_ts',direction='forward',tolerance=pd.Timedelta('2min'))
 for h in HORIZONS:
  q=spot.copy(); q['lookup_ts']=(q.ts-pd.Timedelta(minutes=h+1)).astype('datetime64[ns, UTC]'); q=q[['lookup_ts','open']].rename(columns={'open':f'exit_{h}'})
  x=pd.merge_asof(x.sort_values('ts'),q.sort_values('lookup_ts'),left_on='ts',right_on='lookup_ts',direction='nearest',tolerance=pd.Timedelta('1min'))
  x[f'fwd_{h}']=x[f'exit_{h}']/x.entry-1
 for c in ['ofi','micro_dev_bps','imb_1','imb_5','imb_10','sell_quote']:
  x[c+'_z']=rz(x[c].astype(float))
 x['imb10_chg']=x.imb_10.diff(); x['bid10_pct']=x.bid_depth_10.pct_change().replace([np.inf,-np.inf],np.nan); x['ask10_pct']=x.ask_depth_10.pct_change().replace([np.inf,-np.inf],np.nan)
 x['ret5']=x.entry.pct_change(); x['ret5_z']=rz(x.ret5); x['impact_per_sell']=x.ret5.abs()/x.sell_quote.replace(0,np.nan); x['impact_q20']=x.impact_per_sell.rolling(288,min_periods=144).quantile(.2); x['spread_med']=x.spread_bps.rolling(288,min_periods=144).median()
 return x

def mask(x,h):
 if h=='H1_SELL_EXHAUST_RECOVERY':return (x.ofi_z<=-2.5)&(x.imb_10<=-.50)&(x.imb10_chg>=.20)&(x.spread_bps<=1.5*x.spread_med)
 if h=='H2_MLOFI_CONTINUATION':return (x.ofi_z>=2.5)&(x.imb_1>0)&(x.imb_5>0)&(x.imb_10>0)&(x.micro_dev_bps>0)&(x.spread_bps<=x.spread_med)
 if h=='H3_MICROPRICE_CONFIRM':return (x.micro_dev_bps>=1.5)&(x.imb_1>=.60)&(x.ofi_z>=1.5)&(x.buy_share>=.60)
 if h=='H4_ABSORPTION_REVERSAL':return (x.sell_quote_z>=2.5)&(x.ret5_z<=-1.5)&(x.impact_per_sell<=x.impact_q20)&(x.bid10_pct>=0)
 if h=='H5_ASK_DEPLETION':return (x.ask10_pct<=-.40)&(x.bid10_pct>=-.10)&(x.ofi_z>=2)&(x.micro_dev_bps>0)
 raise KeyError(h)

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--symbol',required=True,choices=['BTCUSDT','ETHUSDT','SOLUSDT']); a=ap.parse_args(); s=a.symbol
 files=select_files(s)
 if len(files)<9: raise SystemExit(f'not enough eligible files: {len(files)}')
 frames=[]; quality=[]
 for date,path,size in files:
  local=hf_hub_download(REPO,path,repo_type='dataset'); z,q=event_features(local); q.update({'date':date,'path':path,'size':size}); quality.append(q)
  if not z.empty:frames.append(z)
 x=pd.concat(frames,ignore_index=True).sort_values('ts').drop_duplicates('ts',keep='last')
 months=sorted(set(d[:7] for d,_,_ in files)); x=enrich(x,s,months)
 dates=[d for d,_,_ in files]; n=len(dates); a1=max(4,n//2); a2=max(a1+2,a1+(n-a1)//2)
 d1=pd.Timestamp(dates[0],tz='UTC'); d2=pd.Timestamp(dates[a1],tz='UTC'); d3=pd.Timestamp(dates[a2],tz='UTC'); dend=pd.Timestamp(dates[-1],tz='UTC')+pd.Timedelta(days=1)
 disc=x[(x.ts>=d1)&(x.ts<d2)]; oos=x[(x.ts>=d2)&(x.ts<d3)]; unt=x[(x.ts>=d3)&(x.ts<dend)]
 tests=[]; frozen=[]
 for hid in HYPOTHESES:
  dm=mask(disc,hid); om=mask(oos,hid)
  for h in HORIZONS:
   d=met(disc.loc[dm,f'fwd_{h}']); o=met(oos.loc[om,f'fwd_{h}']); sv=gate(d,15) and gate(o,8)
   tests.append({'hypothesis':hid,'horizonMin':h,'discovery':{**d,'pass':gate(d,15)},'oos':{**o,'pass':gate(o,8)},'selectionSurvivor':sv})
   if sv:frozen.append((hid,h))
 finals=[]
 for hid,h in frozen:
  e=unt.loc[mask(unt,hid),['ts',f'fwd_{h}']].dropna().rename(columns={f'fwd_{h}':'gross'}); e=decluster(e,h); g=met(e.gross); nn=met(e.gross-NORMAL_COST); ss=met(e.gross-STRESS_COST)
  finals.append({'hypothesis':hid,'horizonMin':h,'declusteredEvents':len(e),'gross':g,'normalNet':nn,'stressNet':ss})
 qs=bh([f['normalNet']['pOneSided'] or 1 for f in finals])
 for f,qv in zip(finals,qs):
  f['normalNetBHq']=qv; f['productionGatePass']=bool(f['declusteredEvents']>=10 and gate(f['normalNet'],10) and f['stressNet']['mean'] is not None and f['stressNet']['mean']>0 and qv<=.10)
 prod=[f for f in finals if f.get('productionGatePass')]
 result={'engine':'DIRECT_DEPTH20_ROUND_V1','authorization':AUTHORIZATION,'liveTrading':False,'automaticPromotion':False,'status':'COMPLETE','symbol':s,'source':{'repo':REPO,'market':'BINANCE_FUTURES_DEPTH20','target':'BINANCE_SPOT','files':files},'quality':quality,'bars5m':len(x),'windows':{'discovery':[str(d1),str(d2)],'oos':[str(d2),str(d3)],'untouched':[str(d3),str(dend)]},'costs':{'normalRoundTrip':NORMAL_COST,'stressRoundTrip':STRESS_COST},'tests':tests,'selectionSurvivors':frozen,'untouchedResults':finals,'productionPassCount':len(prod),'productionCandidate':bool(prod),'verdict':'PRODUCTION_EDGE_FOUND' if prod else 'NO_PRODUCTION_EDGE'}
 out=pathlib.Path(f'artifacts/direct-depth20/{s}'); out.mkdir(parents=True,exist_ok=True); (out/'verdict.json').write_text(json.dumps(result,indent=2,default=str)); print(json.dumps({'symbol':s,'files':len(files),'bars5m':len(x),'selectionSurvivors':frozen,'untouchedResults':finals,'productionPassCount':len(prod),'verdict':result['verdict']},indent=2,default=str))
if __name__=='__main__':main()
