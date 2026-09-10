#!/usr/bin/env python3
"""Research-only true L2 microstructure round on public reconstructable Binance book data.

Frozen hypotheses, separate Discovery/OOS/Untouched windows, Spot target returns,
conservative costs, de-clustering and BH-FDR. No trading or auto-promotion.
"""
from __future__ import annotations
import csv, heapq, io, json, math, pathlib, urllib.request, zipfile
from collections import defaultdict
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

AUTHORIZATION='RESEARCH_ONLY'
DATASET='MaximumLeverage/crypto-lob-stream'
BASE='https://huggingface.co/datasets/'+DATASET+'/resolve/main/'
SYMBOL='SOLUSDT'; MONTH='2026-07'
DEPTH=f'depth/binance/{SYMBOL}/{MONTH}.parquet'
SNAPS=f'snapshots/binance/{SYMBOL}/{MONTH}.parquet'
TRADES=f'trades/binance/{SYMBOL}/{MONTH}.parquet'
SPOT_KLINES=f'https://data.binance.vision/data/spot/monthly/klines/{SYMBOL}/1m/{SYMBOL}-1m-{MONTH}.zip'
NORMAL_COST=0.0028; STRESS_COST=0.0050
GRID_MS=5*60*1000
HORIZONS_MIN=(15,60,240)

# Frozen before outcome inspection. These are intentionally few and mechanism-led.
HYPOTHESES=[
 {'id':'H1_SELL_EXHAUST_RECOVERY','desc':'extreme negative BBO OFI + negative depth imbalance that is recovering'},
 {'id':'H2_MLOFI_CONTINUATION','desc':'positive BBO OFI + aligned L1/L5/L10 imbalance + positive microprice'},
 {'id':'H3_MICROPRICE_CONFIRM','desc':'microprice premium + strong L1 imbalance + positive OFI'},
 {'id':'H4_ABSORPTION_REVERSAL','desc':'extreme aggressive selling/down move with low price impact and stable/rebuilding bid depth'},
 {'id':'H5_ASK_DEPLETION','desc':'sharp ask-depth depletion with resilient bids + positive OFI/microprice'},
]

def dl(url,path):
 p=pathlib.Path(path); p.parent.mkdir(parents=True,exist_ok=True)
 if p.exists() and p.stat().st_size>0:return p
 req=urllib.request.Request(url,headers={'User-Agent':'tst-true-l2-round-v1/1.0'})
 with urllib.request.urlopen(req,timeout=180) as r,p.open('wb') as f:
  while True:
   b=r.read(4*1024*1024)
   if not b:break
   f.write(b)
 return p

def top(book,side,n):
 if side=='bid': return heapq.nlargest(n,book.items(),key=lambda z:z[0])
 return heapq.nsmallest(n,book.items(),key=lambda z:z[0])

def bbo(bids,asks):
 if not bids or not asks:return None
 bp=max(bids); ap=min(asks)
 if bp>=ap:return None
 return bp,bids[bp],ap,asks[ap]

def bbo_ofi(prev,cur):
 if prev is None or cur is None:return 0.0
 pb,pbv,pa,pav=prev; b,bv,a,av=cur
 # Cont-style best-level OFI contribution between consecutive book events.
 e=0.0
 if b>=pb:e+=bv
 if b<=pb:e-=pbv
 if a<=pa:e-=av
 if a>=pa:e+=pav
 return e

def book_features(ts,bids,asks,ofi_acc):
 bb=bbo(bids,asks)
 if bb is None:return None
 bp,bv,ap,av=bb; mid=(bp+ap)/2; spr=ap-bp
 out={'ts_ms':int(ts),'best_bid':bp,'best_ask':ap,'mid':mid,'spread_bps':spr/mid*10000,'bbo_ofi_5m':ofi_acc}
 for n in (1,5,10):
  bs=top(bids,'bid',n); aa=top(asks,'ask',n)
  bd=sum(v for _,v in bs); ad=sum(v for _,v in aa); den=bd+ad
  out[f'bid_depth_{n}']=bd; out[f'ask_depth_{n}']=ad; out[f'imb_{n}']=(bd-ad)/den if den else np.nan
 den=bv+av; micro=(ap*bv+bp*av)/den if den else mid
 out['micro_dev_bps']=(micro/mid-1)*10000
 return out

def reconstruct(depth_path,snap_path):
 s=pq.read_table(snap_path).to_pandas().sort_values(['timestamp_ms','side','price'])
 snap_groups=[]
 for ts,g in s.groupby('timestamp_ms',sort=True):
  snap_groups.append((int(ts),int(g['last_update_id'].iloc[0]),g[['side','price','quantity']].to_records(index=False)))
 si=0; bids={}; asks={}; synced=False; uid=None; rows=[]; gaps=0; resets=0; events=0; stale=0
 next_emit=None; ofi_acc=0.0; prev_bbo=None

 def emit_until(limit):
  nonlocal next_emit,ofi_acc
  if not synced or next_emit is None:return
  while next_emit<=limit:
   f=book_features(next_emit,bids,asks,ofi_acc)
   if f:rows.append(f)
   ofi_acc=0.0; next_emit+=GRID_MS

 def install_snap(ts,lastid,recs):
  nonlocal bids,asks,synced,uid,next_emit,ofi_acc,prev_bbo,resets
  emit_until(ts)
  bids={}; asks={}
  for side,price,qty in recs:
   q=float(qty); p=float(price)
   if q<=0:continue
   (bids if str(side)=='bid' else asks)[p]=q
  synced=bbo(bids,asks) is not None; uid=int(lastid); prev_bbo=bbo(bids,asks); ofi_acc=0.0; resets+=1
  if synced and next_emit is None: next_emit=((int(ts)//GRID_MS)+1)*GRID_MS

 pf=pq.ParquetFile(depth_path)
 current_key=None; buf=[]
 def process_event(key,recs):
  nonlocal synced,uid,ofi_acc,prev_bbo,gaps,events,stale,si
  ts,firstid,lastid=map(int,key)
  while si<len(snap_groups) and snap_groups[si][0]<=ts:
   st,su,sr=snap_groups[si]; install_snap(st,su,sr); si+=1
  emit_until(ts-1)
  if not synced:return
  if lastid<=uid: stale+=1; return
  if firstid>uid+1:
   gaps+=1; synced=False; return
  before=prev_bbo
  for side,price,qty in recs:
   p=float(price); q=float(qty); book=bids if str(side)=='bid' else asks
   if q<=0:book.pop(p,None)
   else:book[p]=q
  after=bbo(bids,asks)
  if after is None:
   synced=False; return
  ofi_acc+=bbo_ofi(before,after); prev_bbo=after; uid=lastid; events+=1

 for batch in pf.iter_batches(batch_size=250000,columns=['timestamp_ms','side','price','quantity','first_update_id','last_update_id']):
  d=batch.to_pydict(); L=len(d['timestamp_ms'])
  for i in range(L):
   key=(d['timestamp_ms'][i],d['first_update_id'][i],d['last_update_id'][i])
   rec=(d['side'][i],d['price'][i],d['quantity'][i])
   if current_key is None:current_key=key
   if key!=current_key:
    process_event(current_key,buf); current_key=key; buf=[]
   buf.append(rec)
 if current_key is not None:process_event(current_key,buf)
 if next_emit is not None and rows: emit_until(rows[-1]['ts_ms']+GRID_MS)
 return pd.DataFrame(rows),{'depthRows':pf.metadata.num_rows,'snapshots':len(snap_groups),'snapshotResets':resets,'eventsApplied':events,'sequenceGaps':gaps,'staleEvents':stale,'featureRows':len(rows)}

def load_trades(path):
 t=pq.read_table(path,columns=['timestamp_ms','price','quantity','buyer_maker']).to_pandas()
 t['ts']=pd.to_datetime(t.timestamp_ms,unit='ms',utc=True); t['quote']=t.price*t.quantity
 t['buy_quote']=np.where(~t.buyer_maker,t['quote'],0.0); t['sell_quote']=np.where(t.buyer_maker,t['quote'],0.0)
 x=t.set_index('ts').resample('5min',label='right',closed='right').agg({'price':'last','buy_quote':'sum','sell_quote':'sum'})
 x['aggr_sell_ratio']=x.sell_quote/(x.buy_quote+x.sell_quote).replace(0,np.nan)
 return x.reset_index()

def load_spot(url):
 raw=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'tst-true-l2-round-v1/1.0'}),timeout=120).read()
 with zipfile.ZipFile(io.BytesIO(raw)) as z:
  name=[n for n in z.namelist() if n.endswith('.csv')][0]
  a=pd.read_csv(z.open(name),header=None,usecols=[0,1,4],names=['open_ts','open','close'])
 a['open_ts']=pd.to_numeric(a.open_ts,errors='coerce'); unit='us' if a.open_ts.dropna().median()>1e14 else 'ms'
 a['ts']=pd.to_datetime(a.open_ts,unit=unit,utc=True,errors='coerce'); a['open']=pd.to_numeric(a.open,errors='coerce'); a['close']=pd.to_numeric(a.close,errors='coerce')
 return a.dropna().sort_values('ts')[['ts','open','close']]

def roll_z(s,w=288,minp=144):
 m=s.rolling(w,min_periods=minp).mean(); sd=s.rolling(w,min_periods=minp).std(ddof=0).replace(0,np.nan); return (s-m)/sd

def metrics(v):
 v=pd.Series(v).dropna().astype(float)
 if len(v)==0:return {'n':0,'mean':None,'median':None,'hitRate':None,'profitFactor':None,'pOneSided':None}
 pos=v[v>0].sum(); neg=-v[v<0].sum(); pf=float(pos/neg) if neg>0 else 999.0
 if len(v)>1 and v.std(ddof=1)>0:
  z=float(v.mean()/(v.std(ddof=1)/math.sqrt(len(v)))); p=0.5*math.erfc(z/math.sqrt(2))
 else:p=1.0
 return {'n':int(len(v)),'mean':float(v.mean()),'median':float(v.median()),'hitRate':float((v>0).mean()),'profitFactor':pf,'pOneSided':float(p)}

def raw_gate(m,minn):
 return m['n']>=minn and m['mean'] is not None and m['mean']>0 and m['median']>0 and m['hitRate']>0.55 and m['profitFactor']>=1.2

def bh(ps):
 n=len(ps); order=sorted(range(n),key=lambda i:ps[i]); q=[1.0]*n; prev=1.0
 for rank_idx in range(n-1,-1,-1):
  i=order[rank_idx]; rank=rank_idx+1; val=min(prev,ps[i]*n/rank); q[i]=val; prev=val
 return q

def decluster(df,hmin):
 if df.empty:return df
 keep=[]; last=None; gap=pd.Timedelta(minutes=hmin)
 for idx,row in df.sort_values('ts').iterrows():
  if last is None or row.ts-last>=gap: keep.append(idx); last=row.ts
 return df.loc[keep]

def prepare(l2,tr,spot):
 x=l2.copy(); x['ts']=pd.to_datetime(x.ts_ms,unit='ms',utc=True); x=x.drop(columns='ts_ms').sort_values('ts')
 x=pd.merge_asof(x,tr.sort_values('ts'),on='ts',direction='backward',tolerance=pd.Timedelta('5min'))
 # Exact/nearest next-minute Spot entry after each L2 grid timestamp.
 sp=spot.copy(); sp['entry_ts']=sp.ts-pd.Timedelta(minutes=1)
 x=pd.merge_asof(x,sp[['entry_ts','open']].rename(columns={'open':'entry'}).sort_values('entry_ts'),left_on='ts',right_on='entry_ts',direction='forward',tolerance=pd.Timedelta('2min'))
 # forward price from Spot opens, no futures target leakage
 for h in HORIZONS_MIN:
  target=sp[['ts','open']].copy(); target['lookup_ts']=target.ts-pd.Timedelta(minutes=h+1)
  target=target[['lookup_ts','open']].rename(columns={'open':f'exit_{h}m'})
  x=pd.merge_asof(x.sort_values('ts'),target.sort_values('lookup_ts'),left_on='ts',right_on='lookup_ts',direction='nearest',tolerance=pd.Timedelta('1min'))
  x[f'fwd_{h}m']=x[f'exit_{h}m']/x.entry-1
 for c in ['bbo_ofi_5m','micro_dev_bps','imb_1','imb_5','imb_10','aggr_sell_ratio']:
  x[c+'_z']=roll_z(x[c].astype(float))
 x['imb10_chg']=x.imb_10.diff(); x['bid10_pct']=x.bid_depth_10.pct_change().replace([np.inf,-np.inf],np.nan); x['ask10_pct']=x.ask_depth_10.pct_change().replace([np.inf,-np.inf],np.nan)
 x['ret5']=x.entry.pct_change(); x['ret5_z']=roll_z(x.ret5)
 x['sell_quote_z']=roll_z(x.sell_quote.astype(float))
 x['impact_per_sell']=x.ret5.abs()/x.sell_quote.replace(0,np.nan)
 x['impact_q20']=x.impact_per_sell.rolling(288,min_periods=144).quantile(.2)
 return x

def event_mask(x,h):
 if h=='H1_SELL_EXHAUST_RECOVERY': return (x.bbo_ofi_5m_z<=-2.5)&(x.imb_10<=-.50)&(x.imb10_chg>=.20)
 if h=='H2_MLOFI_CONTINUATION': return (x.bbo_ofi_5m_z>=2.5)&(x.imb_1>0)&(x.imb_5>0)&(x.imb_10>0)&(x.micro_dev_bps>0)
 if h=='H3_MICROPRICE_CONFIRM': return (x.micro_dev_bps>=1.5)&(x.imb_1>=.60)&(x.bbo_ofi_5m_z>=1.5)
 if h=='H4_ABSORPTION_REVERSAL': return (x.sell_quote_z>=2.5)&(x.ret5_z<=-1.5)&(x.impact_per_sell<=x.impact_q20)&(x.bid10_pct>=0)
 if h=='H5_ASK_DEPLETION': return (x.ask10_pct<=-.40)&(x.bid10_pct>=-.10)&(x.bbo_ofi_5m_z>=2.0)&(x.micro_dev_bps>0)
 raise KeyError(h)

def main():
 out=pathlib.Path('artifacts/true-l2-round-v1'); out.mkdir(parents=True,exist_ok=True); data=pathlib.Path('/tmp/l2')
 dp=dl(BASE+DEPTH,data/'depth.parquet'); sn=dl(BASE+SNAPS,data/'snapshots.parquet'); tp=dl(BASE+TRADES,data/'trades.parquet')
 try:
  readme=urllib.request.urlopen(BASE+'README.md',timeout=60).read().decode('utf-8','replace')
 except Exception as e:readme='UNAVAILABLE '+repr(e)
 l2,quality=reconstruct(dp,sn); tr=load_trades(tp); spot=load_spot(SPOT_KLINES); x=prepare(l2,tr,spot)
 # Chronological, predeclared 3-way split; first 24h is effectively rolling warm-up.
 disc=x[(x.ts>=pd.Timestamp('2026-07-02',tz='UTC'))&(x.ts<pd.Timestamp('2026-07-15',tz='UTC'))]
 oos=x[(x.ts>=pd.Timestamp('2026-07-15',tz='UTC'))&(x.ts<pd.Timestamp('2026-07-23',tz='UTC'))]
 unt=x[(x.ts>=pd.Timestamp('2026-07-23',tz='UTC'))&(x.ts<pd.Timestamp('2026-08-01',tz='UTC'))]
 tests=[]; frozen=[]
 for hh in HYPOTHESES:
  hid=hh['id']; dm=event_mask(disc,hid); om=event_mask(oos,hid)
  for horizon in HORIZONS_MIN:
   md=metrics(disc.loc[dm,f'fwd_{horizon}m']); mo=metrics(oos.loc[om,f'fwd_{horizon}m']); survive=raw_gate(md,20) and raw_gate(mo,15)
   rec={'hypothesis':hid,'horizonMin':horizon,'discovery':{**md,'pass':raw_gate(md,20)},'oos':{**mo,'pass':raw_gate(mo,15)},'selectionSurvivor':bool(survive)}; tests.append(rec)
   if survive:frozen.append((hid,horizon))
 finals=[]
 for hid,horizon in frozen:
  m=event_mask(unt,hid); e=unt.loc[m,['ts',f'fwd_{horizon}m']].dropna().copy(); e['gross']=e[f'fwd_{horizon}m']; e=decluster(e,horizon)
  g=metrics(e.gross); nn=metrics(e.gross-NORMAL_COST); ss=metrics(e.gross-STRESS_COST)
  finals.append({'hypothesis':hid,'horizonMin':horizon,'declusteredEvents':len(e),'gross':g,'normalNet':nn,'stressNet':ss})
 if finals:
  qs=bh([f['normalNet']['pOneSided'] if f['normalNet']['pOneSided'] is not None else 1 for f in finals])
  for f,q in zip(finals,qs):
   f['normalNetBHq']=float(q); f['productionGatePass']=bool(f['declusteredEvents']>=15 and raw_gate(f['normalNet'],15) and f['stressNet']['mean'] is not None and f['stressNet']['mean']>0 and q<=.10)
 prod=[f for f in finals if f.get('productionGatePass')]
 market_hint='unknown'; low=readme.lower()
 if 'futures' in low or 'perpetual' in low: market_hint='README_MENTIONS_FUTURES_OR_PERPETUAL'
 elif 'spot' in low: market_hint='README_MENTIONS_SPOT'
 result={'engine':'TRUE_L2_MICROSTRUCTURE_ROUND_V1','authorization':AUTHORIZATION,'liveTrading':False,'automaticPromotion':False,'status':'COMPLETE','source':{'dataset':DATASET,'files':[DEPTH,SNAPS,TRADES],'marketHint':market_hint,'targetMarket':'BINANCE_SPOT','spotTargetSource':'Binance Vision Spot 1m klines'},'quality':quality,'windows':{'discovery':'2026-07-02..2026-07-14','oos':'2026-07-15..2026-07-22','untouched':'2026-07-23..2026-07-31'},'costs':{'normalRoundTrip':NORMAL_COST,'stressRoundTrip':STRESS_COST},'hypotheses':HYPOTHESES,'tests':tests,'selectionSurvivorCount':len(frozen),'untouchedResults':finals,'productionPassCount':len(prod),'productionCandidate':bool(prod),'verdict':'PRODUCTION_EDGE_FOUND' if prod else 'REJECT_TRUE_L2_FAMILY_IN_THIS_ROUND','notes':['BBO OFI is accumulated from reconstructed event-level best-book changes.','L1/L5/L10 imbalance and microprice are computed from reconstructed book state.','Spot forward returns are sourced independently from Binance Vision Spot klines.','No TP/SL or sizing optimization.']}
 (out/'true-l2-round-v1-verdict.json').write_text(json.dumps(result,indent=2,default=str))
 print(json.dumps({'status':'COMPLETE','quality':quality,'selectionSurvivors':len(frozen),'productionPass':len(prod),'survivors':frozen,'verdict':result['verdict']},indent=2))
if __name__=='__main__':main()
