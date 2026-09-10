#!/usr/bin/env python3
"""Canonical full-L2 microstructure research round.
Uses public crypto-lob-stream Parquet data, replayed with absolute level replacement,
1000-level pruning and snapshot hard resets. Research only; no live actions.
"""
from __future__ import annotations
import argparse, io, json, math, pathlib, urllib.request, zipfile
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from sortedcontainers import SortedDict

AUTHORIZATION='RESEARCH_ONLY'
DATASET='MaximumLeverage/crypto-lob-stream'
BASE='https://huggingface.co/datasets/'+DATASET+'/resolve/main/'
MONTH='2026-07'; MAX_DEPTH=1000; GRID_MS=5*60*1000
NORMAL_COST=0.0028; STRESS_COST=0.0050
HORIZONS=(15,60,240)
HYPOTHESES=[
 {'id':'H1_SELL_EXHAUST_RECOVERY','desc':'extreme negative BBO OFI + negative depth imbalance recovering'},
 {'id':'H2_BBO_OFI_MULTI_DEPTH_CONTINUATION','desc':'positive BBO OFI + aligned L1/L5/L10 imbalance + positive microprice'},
 {'id':'H3_MICROPRICE_CONFIRM','desc':'microprice premium + strong L1 imbalance + positive BBO OFI'},
 {'id':'H4_ABSORPTION_REVERSAL','desc':'extreme aggressive selling/down move with low impact and resilient bid depth'},
 {'id':'H5_ASK_DEPLETION','desc':'sharp ask-depth depletion with resilient bids + positive OFI/microprice'},
]

def dl(url,path):
 p=pathlib.Path(path); p.parent.mkdir(parents=True,exist_ok=True)
 if p.exists() and p.stat().st_size:return p
 req=urllib.request.Request(url,headers={'User-Agent':'tst-true-l2-v2/1.0'})
 with urllib.request.urlopen(req,timeout=240) as r,p.open('wb') as f:
  while True:
   b=r.read(8*1024*1024)
   if not b: break
   f.write(b)
 return p

def prune(book,side):
 while len(book)>MAX_DEPTH:
  book.popitem(index=0 if side=='bid' else -1)

def apply_level(book,side,price,qty):
 p=float(price); q=float(qty)
 if q==0: book.pop(p,None)
 else: book[p]=q
 prune(book,side)

def bbo(bids,asks):
 if not bids or not asks:return None
 bp=bids.peekitem(-1)[0]; ap=asks.peekitem(0)[0]
 if bp>=ap:return None
 return bp,bids[bp],ap,asks[ap]

def ofi(prev,cur):
 if prev is None or cur is None:return 0.0
 pb,pbv,pa,pav=prev; b,bv,a,av=cur; e=0.0
 if b>=pb:e+=bv
 if b<=pb:e-=pbv
 if a<=pa:e-=av
 if a>=pa:e+=pav
 return e

def feat(ts,bids,asks,ofi5):
 bb=bbo(bids,asks)
 if bb is None:return None
 bp,bv,ap,av=bb; mid=(bp+ap)/2
 d={'ts_ms':int(ts),'best_bid':bp,'best_ask':ap,'mid':mid,'spread_bps':(ap-bp)/mid*10000,'bbo_ofi_5m':ofi5}
 for n in (1,5,10):
  bi=list(reversed(bids.items()))[:n]; ai=list(asks.items())[:n]
  bd=sum(v for _,v in bi); ad=sum(v for _,v in ai); den=bd+ad
  d[f'bid_depth_{n}']=bd; d[f'ask_depth_{n}']=ad; d[f'imb_{n}']=(bd-ad)/den if den else np.nan
 den=bv+av; mp=(ap*bv+bp*av)/den if den else mid
 d['micro_dev_bps']=(mp/mid-1)*10000
 return d

def replay(depth_path,snap_path):
 st=pq.read_table(snap_path).to_pandas().sort_values(['timestamp_ms','last_update_id','side','price'])
 snaps=[]
 for ts,g in st.groupby('timestamp_ms',sort=True):
  snaps.append((int(ts),int(g.last_update_id.iloc[0]),g[['side','price','quantity']].to_records(index=False)))
 si=0; bids=SortedDict(); asks=SortedDict(); anchor=None; active=False
 next_emit=None; ofi5=0.0; prev=None; rows=[]; groups=0; applied=0; stale=0; resets=0; crossed=0

 def load_snapshot(ts,lastid,recs):
  nonlocal bids,asks,anchor,active,next_emit,ofi5,prev,resets
  bids=SortedDict(); asks=SortedDict()
  for side,price,qty in recs:
   book=bids if str(side)=='bid' else asks
   if float(qty)>0: book[float(price)]=float(qty)
  while len(bids)>MAX_DEPTH:bids.popitem(0)
  while len(asks)>MAX_DEPTH:asks.popitem(-1)
  anchor=lastid; active=bbo(bids,asks) is not None; prev=bbo(bids,asks); ofi5=0.0; resets+=1
  if next_emit is None:next_emit=((ts//GRID_MS)+1)*GRID_MS

 def emit_before(ts):
  nonlocal next_emit,ofi5
  if next_emit is None:return
  while next_emit<ts:
   if active:
    z=feat(next_emit,bids,asks,ofi5)
    if z:rows.append(z)
   ofi5=0.0; next_emit+=GRID_MS

 def emit_equal(ts):
  nonlocal next_emit,ofi5
  if next_emit is not None and next_emit==ts:
   if active:
    z=feat(next_emit,bids,asks,ofi5)
    if z:rows.append(z)
   ofi5=0.0; next_emit+=GRID_MS

 current=None; buf=[]
 def process(key,recs):
  nonlocal si,groups,applied,stale,active,prev,ofi5,crossed,anchor
  ts,fi,li=map(int,key); groups+=1
  emit_before(ts)
  while si<len(snaps) and snaps[si][0]<=ts:
   stime,sid,srows=snaps[si]
   # hard reset at every capture snapshot/reconnect anchor
   load_snapshot(stime,sid,srows); si+=1
  if not active or anchor is None:
   emit_equal(ts); return
  if li<=anchor:
   stale+=1; emit_equal(ts); return
  before=prev
  for side,price,qty in recs:
   apply_level(bids if str(side)=='bid' else asks,str(side),price,qty)
  cur=bbo(bids,asks)
  if cur is None:
   crossed+=1; active=False
  else:
   ofi5+=ofi(before,cur); prev=cur; anchor=max(anchor,li); applied+=1
  emit_equal(ts)

 pf=pq.ParquetFile(depth_path)
 for batch in pf.iter_batches(batch_size=200000,columns=['timestamp_ms','side','price','quantity','first_update_id','last_update_id']):
  d=batch.to_pydict()
  for ts,side,price,qty,fi,li in zip(d['timestamp_ms'],d['side'],d['price'],d['quantity'],d['first_update_id'],d['last_update_id']):
   key=(int(ts),int(fi),int(li)); rec=(side,price,qty)
   if current is None:current=key
   if key!=current:
    process(current,buf); current=key; buf=[]
   buf.append(rec)
 if current is not None:process(current,buf)
 return pd.DataFrame(rows),{'depthRows':pf.metadata.num_rows,'eventGroups':groups,'eventsApplied':applied,'staleGroups':stale,'snapshotGroups':len(snaps),'snapshotResets':resets,'crossedBookResets':crossed,'featureRows':len(rows),'maxDepth':MAX_DEPTH}

def load_trades(path):
 t=pq.read_table(path,columns=['timestamp_ms','price','quantity','buyer_maker']).to_pandas()
 t['ts']=pd.to_datetime(t.timestamp_ms,unit='ms',utc=True).astype('datetime64[ns, UTC]'); t['quote']=t.price*t.quantity
 t['buy_quote']=np.where(~t.buyer_maker,t.quote,0.0); t['sell_quote']=np.where(t.buyer_maker,t.quote,0.0)
 x=t.set_index('ts').resample('5min',label='right',closed='right').agg({'price':'last','buy_quote':'sum','sell_quote':'sum'}).reset_index()
 x['aggr_sell_ratio']=x.sell_quote/(x.buy_quote+x.sell_quote).replace(0,np.nan)
 return x

def load_spot(symbol):
 url=f'https://data.binance.vision/data/spot/monthly/klines/{symbol}/1m/{symbol}-1m-{MONTH}.zip'
 raw=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'tst-true-l2-v2/1.0'}),timeout=180).read()
 with zipfile.ZipFile(io.BytesIO(raw)) as z:
  n=[n for n in z.namelist() if n.endswith('.csv')][0]
  a=pd.read_csv(z.open(n),header=None,usecols=[0,1],names=['open_ts','open'])
 a.open_ts=pd.to_numeric(a.open_ts,errors='coerce'); unit='us' if a.open_ts.dropna().median()>1e14 else 'ms'
 a['ts']=pd.to_datetime(a.open_ts,unit=unit,utc=True,errors='coerce').astype('datetime64[ns, UTC]'); a.open=pd.to_numeric(a.open,errors='coerce')
 return a.dropna().sort_values('ts')[['ts','open']]

def rz(s,w=288,minp=144):
 m=s.rolling(w,min_periods=minp).mean(); sd=s.rolling(w,min_periods=minp).std(ddof=0).replace(0,np.nan); return (s-m)/sd

def prepare(l2,tr,sp):
 x=l2.copy(); x['ts']=pd.to_datetime(x.ts_ms,unit='ms',utc=True).astype('datetime64[ns, UTC]'); x=x.drop(columns='ts_ms').sort_values('ts')
 x=pd.merge_asof(x,tr.sort_values('ts'),on='ts',direction='backward',tolerance=pd.Timedelta('5min'))
 right=sp.copy(); right['entry_ts']=(right.ts-pd.Timedelta(minutes=1)).astype('datetime64[ns, UTC]')
 x=pd.merge_asof(x,right[['entry_ts','open']].rename(columns={'open':'entry'}).sort_values('entry_ts'),left_on='ts',right_on='entry_ts',direction='forward',tolerance=pd.Timedelta('2min'))
 for h in HORIZONS:
  q=sp.copy(); q['lookup_ts']=(q.ts-pd.Timedelta(minutes=h+1)).astype('datetime64[ns, UTC]'); q=q[['lookup_ts','open']].rename(columns={'open':f'exit_{h}'})
  x=pd.merge_asof(x.sort_values('ts'),q.sort_values('lookup_ts'),left_on='ts',right_on='lookup_ts',direction='nearest',tolerance=pd.Timedelta('1min'))
  x[f'fwd_{h}']=x[f'exit_{h}']/x.entry-1
 for c in ['bbo_ofi_5m','micro_dev_bps','imb_1','imb_5','imb_10','aggr_sell_ratio']:
  x[c+'_z']=rz(x[c].astype(float))
 x['imb10_chg']=x.imb_10.diff(); x['bid10_pct']=x.bid_depth_10.pct_change().replace([np.inf,-np.inf],np.nan); x['ask10_pct']=x.ask_depth_10.pct_change().replace([np.inf,-np.inf],np.nan)
 x['ret5']=x.entry.pct_change(); x['ret5_z']=rz(x.ret5); x['sell_quote_z']=rz(x.sell_quote.astype(float)); x['impact_per_sell']=x.ret5.abs()/x.sell_quote.replace(0,np.nan); x['impact_q20']=x.impact_per_sell.rolling(288,min_periods=144).quantile(.2)
 return x

def mask(x,h):
 if h=='H1_SELL_EXHAUST_RECOVERY':return (x.bbo_ofi_5m_z<=-2.5)&(x.imb_10<=-.50)&(x.imb10_chg>=.20)
 if h=='H2_BBO_OFI_MULTI_DEPTH_CONTINUATION':return (x.bbo_ofi_5m_z>=2.5)&(x.imb_1>0)&(x.imb_5>0)&(x.imb_10>0)&(x.micro_dev_bps>0)
 if h=='H3_MICROPRICE_CONFIRM':return (x.micro_dev_bps>=1.5)&(x.imb_1>=.60)&(x.bbo_ofi_5m_z>=1.5)
 if h=='H4_ABSORPTION_REVERSAL':return (x.sell_quote_z>=2.5)&(x.ret5_z<=-1.5)&(x.impact_per_sell<=x.impact_q20)&(x.bid10_pct>=0)
 if h=='H5_ASK_DEPLETION':return (x.ask10_pct<=-.40)&(x.bid10_pct>=-.10)&(x.bbo_ofi_5m_z>=2)&(x.micro_dev_bps>0)
 raise KeyError(h)

def met(v):
 v=pd.Series(v).dropna().astype(float)
 if len(v)==0:return {'n':0,'mean':None,'median':None,'hitRate':None,'profitFactor':None,'pOneSided':None}
 pos=v[v>0].sum(); neg=-v[v<0].sum(); pf=float(pos/neg) if neg>0 else 999.0
 if len(v)>1 and v.std(ddof=1)>0:
  z=float(v.mean()/(v.std(ddof=1)/math.sqrt(len(v)))); p=.5*math.erfc(z/math.sqrt(2))
 else:p=1.0
 return {'n':int(len(v)),'mean':float(v.mean()),'median':float(v.median()),'hitRate':float((v>0).mean()),'profitFactor':pf,'pOneSided':float(p)}

def gate(m,n):return m['n']>=n and m['mean'] is not None and m['mean']>0 and m['median']>0 and m['hitRate']>.55 and m['profitFactor']>=1.2

def decluster(e,h):
 keep=[]; last=None; gap=pd.Timedelta(minutes=h)
 for i,r in e.sort_values('ts').iterrows():
  if last is None or r.ts-last>=gap:keep.append(i); last=r.ts
 return e.loc[keep]

def bh(ps):
 n=len(ps); order=sorted(range(n),key=lambda i:ps[i]); q=[1.]*n; prev=1.
 for j in range(n-1,-1,-1):
  i=order[j]; prev=min(prev,ps[i]*n/(j+1)); q[i]=prev
 return q

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--symbol',required=True,choices=['BTCUSDT','ETHUSDT','SOLUSDT']); a=ap.parse_args(); s=a.symbol
 root=pathlib.Path('/tmp/l2v2'); dp=dl(BASE+f'depth/binance/{s}/{MONTH}.parquet',root/f'{s}-depth.parquet'); sn=dl(BASE+f'snapshots/binance/{s}/{MONTH}.parquet',root/f'{s}-snap.parquet'); tp=dl(BASE+f'trades/binance/{s}/{MONTH}.parquet',root/f'{s}-trades.parquet')
 l2,quality=replay(dp,sn); x=prepare(l2,load_trades(tp),load_spot(s))
 disc=x[(x.ts>=pd.Timestamp('2026-07-02',tz='UTC'))&(x.ts<pd.Timestamp('2026-07-15',tz='UTC'))]; oos=x[(x.ts>=pd.Timestamp('2026-07-15',tz='UTC'))&(x.ts<pd.Timestamp('2026-07-23',tz='UTC'))]; unt=x[(x.ts>=pd.Timestamp('2026-07-23',tz='UTC'))&(x.ts<pd.Timestamp('2026-08-01',tz='UTC'))]
 tests=[]; frozen=[]
 for hh in HYPOTHESES:
  hid=hh['id']; dm=mask(disc,hid); om=mask(oos,hid)
  for h in HORIZONS:
   d=met(disc.loc[dm,f'fwd_{h}']); o=met(oos.loc[om,f'fwd_{h}']); sv=gate(d,20) and gate(o,15); tests.append({'hypothesis':hid,'horizonMin':h,'discovery':{**d,'pass':gate(d,20)},'oos':{**o,'pass':gate(o,15)},'selectionSurvivor':sv})
   if sv:frozen.append((hid,h))
 finals=[]
 for hid,h in frozen:
  e=unt.loc[mask(unt,hid),['ts',f'fwd_{h}']].dropna().rename(columns={f'fwd_{h}':'gross'}); e=decluster(e,h); g=met(e.gross); n=met(e.gross-NORMAL_COST); st=met(e.gross-STRESS_COST); finals.append({'hypothesis':hid,'horizonMin':h,'declusteredEvents':len(e),'gross':g,'normalNet':n,'stressNet':st})
 if finals:
  qs=bh([z['normalNet']['pOneSided'] or 1 for z in finals])
  for z,q in zip(finals,qs):z['normalNetBHq']=q; z['productionGatePass']=bool(z['declusteredEvents']>=15 and gate(z['normalNet'],15) and z['stressNet']['mean'] is not None and z['stressNet']['mean']>0 and q<=.10)
 prod=[z for z in finals if z.get('productionGatePass')]
 r={'engine':'TRUE_L2_MICROSTRUCTURE_ROUND_V2','authorization':AUTHORIZATION,'liveTrading':False,'automaticPromotion':False,'status':'COMPLETE','symbol':s,'source':{'dataset':DATASET,'month':MONTH,'featureMarket':'BINANCE_L2_DATASET','targetMarket':'BINANCE_SPOT','replaySemantics':'absolute replacement; snapshot hard reset; last_update_id file order; max depth 1000'},'quality':quality,'windows':{'discovery':'2026-07-02..14','oos':'2026-07-15..22','untouched':'2026-07-23..31'},'costs':{'normalRoundTrip':NORMAL_COST,'stressRoundTrip':STRESS_COST},'hypotheses':HYPOTHESES,'tests':tests,'selectionSurvivorCount':len(frozen),'untouchedResults':finals,'productionPassCount':len(prod),'productionCandidate':bool(prod),'verdict':'PRODUCTION_EDGE_FOUND' if prod else 'NO_PRODUCTION_EDGE'}
 out=pathlib.Path(f'artifacts/true-l2-v2/{s}'); out.mkdir(parents=True,exist_ok=True); (out/'verdict.json').write_text(json.dumps(r,indent=2)); print(json.dumps({'symbol':s,'quality':quality,'selectionSurvivors':frozen,'productionPassCount':len(prod),'verdict':r['verdict']},indent=2))
if __name__=='__main__':main()
