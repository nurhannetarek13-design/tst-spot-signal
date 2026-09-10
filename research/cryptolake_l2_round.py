#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, math, pathlib, urllib.request, os
from datetime import date, timedelta
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

AUTHORIZATION='RESEARCH_ONLY'
REPO='delmiron27/cryptolake-binance-futures-sol'
SYMBOL='SOL-USDT-PERP'
BASE=f'https://huggingface.co/datasets/{REPO}/resolve/main/'
NORMAL_COST=.0028; STRESS_COST=.0050; HORIZONS=(15,60,240)
HYPOTHESES=['H1_SELL_EXHAUST_RECOVERY','H2_MULTI_DEPTH_CONTINUATION','H3_MICROPRICE_CONFIRM','H4_ABSORPTION_REVERSAL','H5_ASK_DEPLETION']


def dl(rel,path):
    p=pathlib.Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    req=urllib.request.Request(BASE+rel,headers={'User-Agent':'tst-cryptolake-round/1.0'})
    with urllib.request.urlopen(req,timeout=240) as r,p.open('wb') as f:
        while True:
            b=r.read(8*1024*1024)
            if not b: break
            f.write(b)
    return p

def to_ts(s):
    y=pd.to_numeric(s,errors='coerce'); med=float(y.dropna().median())
    unit='ns' if med>1e17 else ('us' if med>1e14 else ('ms' if med>1e11 else 's'))
    return pd.to_datetime(y,unit=unit,utc=True,errors='coerce')

def bbo_ofi(df):
    b=df.bid_0_price.astype(float); bv=df.bid_0_size.astype(float); a=df.ask_0_price.astype(float); av=df.ask_0_size.astype(float)
    pb=b.shift(); pbv=bv.shift(); pa=a.shift(); pav=av.shift()
    e=np.where(b>=pb,bv,0.0)-np.where(b<=pb,pbv,0.0)-np.where(a<=pa,av,0.0)+np.where(a>=pa,pav,0.0)
    e[0]=0.0
    return pd.Series(e,index=df.index)

def process_book(path):
    cols=['timestamp','sequence_number']
    for side in ['bid','ask']:
        for i in range(20): cols += [f'{side}_{i}_price',f'{side}_{i}_size']
    d=pq.read_table(path,columns=cols).to_pandas()
    d['ts']=to_ts(d.timestamp); d=d.dropna(subset=['ts']).sort_values(['ts','sequence_number']).drop_duplicates('ts',keep='last')
    valid=(d.bid_0_price>0)&(d.ask_0_price>0)&(d.bid_0_price<d.ask_0_price)
    for i in range(20): valid &= (d[f'bid_{i}_size']>=0)&(d[f'ask_{i}_size']>=0)
    d=d.loc[valid].copy()
    d['mid']=(d.bid_0_price+d.ask_0_price)/2; d['spread_bps']=(d.ask_0_price-d.bid_0_price)/d.mid*10000
    den=d.bid_0_size+d.ask_0_size
    d['microprice']=(d.ask_0_price*d.bid_0_size+d.bid_0_price*d.ask_0_size)/den.replace(0,np.nan)
    d['micro_dev_bps']=(d.microprice/d.mid-1)*10000
    for n in [1,5,10,20]:
        bd=sum(d[f'bid_{i}_size'] for i in range(n)); ad=sum(d[f'ask_{i}_size'] for i in range(n)); tot=bd+ad
        d[f'bid_depth_{n}']=bd; d[f'ask_depth_{n}']=ad; d[f'imb_{n}']=(bd-ad)/tot.replace(0,np.nan)
    d['snapshot_ofi']=bbo_ofi(d)
    d['bid10_pct_event']=d.bid_depth_10.pct_change(fill_method=None).replace([np.inf,-np.inf],np.nan)
    d['ask10_pct_event']=d.ask_depth_10.pct_change(fill_method=None).replace([np.inf,-np.inf],np.nan)
    d=d.set_index('ts')
    state=['mid','spread_bps','micro_dev_bps','bid_depth_1','ask_depth_1','imb_1','bid_depth_5','ask_depth_5','imb_5','bid_depth_10','ask_depth_10','imb_10','bid_depth_20','ask_depth_20','imb_20']
    out=d[state].resample('5min',label='right',closed='right').last()
    out['snapshot_ofi']=d.snapshot_ofi.resample('5min',label='right',closed='right').sum()
    out['bid10_pct']=out.bid_depth_10.pct_change(fill_method=None).replace([np.inf,-np.inf],np.nan)
    out['ask10_pct']=out.ask_depth_10.pct_change(fill_method=None).replace([np.inf,-np.inf],np.nan)
    out['snapshot_count']=d.mid.resample('5min',label='right',closed='right').count()
    return out.reset_index()

def process_trades(path):
    d=pq.read_table(path,columns=['timestamp','side','amount','price']).to_pandas(); d['ts']=to_ts(d.timestamp); d=d.dropna(subset=['ts']).sort_values('ts')
    d['quote']=d.amount.astype(float)*d.price.astype(float); s=d.side.astype(str).str.lower()
    d['buy_quote']=np.where(s.str.startswith('b'),d.quote,0.); d['sell_quote']=np.where(s.str.startswith('s'),d.quote,0.)
    out=d.set_index('ts').resample('5min',label='right',closed='right').agg({'price':'last','buy_quote':'sum','sell_quote':'sum','quote':'sum'}).reset_index()
    out['aggr_sell_ratio']=out.sell_quote/out.quote.replace(0,np.nan)
    return out

def rz(s,w=288,minp=144):
    m=s.rolling(w,min_periods=minp).mean(); sd=s.rolling(w,min_periods=minp).std(ddof=0).replace(0,np.nan); return (s-m)/sd

def build_day(dt,root):
    ds=dt.isoformat(); basebook=f'raw/book/exchange=BINANCE_FUTURES/symbol={SYMBOL}/dt={ds}/1.snappy.parquet'; basetr=f'raw/trades/exchange=BINANCE_FUTURES/symbol={SYMBOL}/dt={ds}/1.snappy.parquet'
    bp=dl(basebook,root/f'{ds}-book.parquet'); tp=dl(basetr,root/f'{ds}-trades.parquet')
    b=process_book(bp); t=process_trades(tp); x=pd.merge_asof(b.sort_values('ts'),t.sort_values('ts'),on='ts',direction='backward',tolerance=pd.Timedelta('5min'))
    try: os.remove(bp); os.remove(tp)
    except OSError: pass
    return x

def prep(x):
    x=x.sort_values('ts').drop_duplicates('ts').reset_index(drop=True)
    for c in ['snapshot_ofi','micro_dev_bps','imb_1','imb_5','imb_10','aggr_sell_ratio','sell_quote']:
        x[c+'_z']=rz(x[c].astype(float))
    x['imb10_chg']=x.imb_10.diff(); x['ret5']=x.mid.pct_change(fill_method=None); x['ret5_z']=rz(x.ret5)
    x['impact_per_sell']=x.ret5.abs()/x.sell_quote.replace(0,np.nan); x['impact_q20']=x.impact_per_sell.rolling(288,min_periods=144).quantile(.2)
    for h in HORIZONS: x[f'fwd_{h}']=x.mid.shift(-h//5)/x.mid-1
    return x

def mask(x,h):
    if h=='H1_SELL_EXHAUST_RECOVERY': return (x.snapshot_ofi_z<=-2.5)&(x.imb_10<=-.50)&(x.imb10_chg>=.20)
    if h=='H2_MULTI_DEPTH_CONTINUATION': return (x.snapshot_ofi_z>=2.5)&(x.imb_1>0)&(x.imb_5>0)&(x.imb_10>0)&(x.micro_dev_bps>0)
    if h=='H3_MICROPRICE_CONFIRM': return (x.micro_dev_bps>=1.5)&(x.imb_1>=.60)&(x.snapshot_ofi_z>=1.5)
    if h=='H4_ABSORPTION_REVERSAL': return (x.sell_quote_z>=2.5)&(x.ret5_z<=-1.5)&(x.impact_per_sell<=x.impact_q20)&(x.bid10_pct>=0)
    if h=='H5_ASK_DEPLETION': return (x.ask10_pct<=-.40)&(x.bid10_pct>=-.10)&(x.snapshot_ofi_z>=2)&(x.micro_dev_bps>0)
    raise KeyError(h)

def met(v):
    v=pd.Series(v).dropna().astype(float)
    if not len(v): return {'n':0,'mean':None,'median':None,'hitRate':None,'profitFactor':None}
    pos=v[v>0].sum(); neg=-v[v<0].sum(); pf=float(pos/neg) if neg>0 else 999.
    return {'n':int(len(v)),'mean':float(v.mean()),'median':float(v.median()),'hitRate':float((v>0).mean()),'profitFactor':pf}

def gate(m,n=10): return m['n']>=n and m['mean'] is not None and m['mean']>0 and m['median']>0 and m['hitRate']>.55 and m['profitFactor']>=1.2

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--start',default='2025-01-09'); ap.add_argument('--days',type=int,default=7); a=ap.parse_args()
    start=date.fromisoformat(a.start); root=pathlib.Path('/tmp/cryptolake-round'); frames=[]; daily=[]
    for i in range(a.days):
        dt=start+timedelta(days=i); x=build_day(dt,root); daily.append({'date':dt.isoformat(),'rows5m':len(x),'snapshotMin':int(x.snapshot_count.min()),'snapshotMedian':float(x.snapshot_count.median()),'snapshotMax':int(x.snapshot_count.max())}); frames.append(x)
    x=prep(pd.concat(frames,ignore_index=True)); tests=[]
    for hid in HYPOTHESES:
        m=mask(x,hid)
        for h in HORIZONS:
            z=met(x.loc[m,f'fwd_{h}']); tests.append({'hypothesis':hid,'horizonMin':h,'gross':z,'grossGatePass':gate(z,10),'normalNetMean':None if z['mean'] is None else z['mean']-NORMAL_COST,'stressNetMean':None if z['mean'] is None else z['mean']-STRESS_COST})
    r={'engine':'CRYPTOLAKE_L2_SNAPSHOT_SMOKE_V1','authorization':AUTHORIZATION,'liveTrading':False,'automaticPromotion':False,'source':REPO,'symbol':SYMBOL,'start':a.start,'days':a.days,'featureSemantics':{'ofi':'snapshot_ofi proxy from consecutive materialized L2 snapshots; not diff-stream canonical OFI','book':'20-level materialized Binance Futures snapshots','tradeFlow':'Cryptolake trades side field'},'dailyQuality':daily,'tests':tests,'status':'COMPLETE'}
    out=pathlib.Path('artifacts/cryptolake-l2-smoke'); out.mkdir(parents=True,exist_ok=True); (out/'verdict.json').write_text(json.dumps(r,indent=2)); print(json.dumps(r,indent=2))
if __name__=='__main__': main()
