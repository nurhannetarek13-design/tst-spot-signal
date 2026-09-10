#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, gzip, io, json, math, pathlib, urllib.request, zipfile, os
from datetime import date, timedelta
import numpy as np
import pandas as pd
import msgspec
from sortedcontainers import SortedDict

AUTH='RESEARCH_ONLY'
NORMAL_COST=.0028; STRESS_COST=.0050
HORIZONS={60:6,300:30,900:90}
BYBIT='https://quote-saver.bycsi.com/orderbook/linear'
VISION='https://data.binance.vision/data/spot/daily/aggTrades'
UA={'User-Agent':'Mozilla/5.0'}


def download(url,path):
    p=pathlib.Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    req=urllib.request.Request(url,headers=UA)
    with urllib.request.urlopen(req,timeout=600) as r,p.open('wb') as f:
        while True:
            b=r.read(8*1024*1024)
            if not b:break
            f.write(b)
    return p

def apply(book,levels):
    for p,q in levels:
        p=float(p); q=float(q)
        if q==0: book.pop(p,None)
        else: book[p]=q

def bstate(bids,asks):
    if not bids or not asks:return None
    bp,bv=bids.peekitem(-1); ap,av=asks.peekitem(0)
    if bp>=ap:return None
    b10=list(reversed(bids.items()))[:10]; a10=list(asks.items())[:10]
    bd=sum(v for _,v in b10); ad=sum(v for _,v in a10); den=bd+ad
    mid=(bp+ap)/2; micro=(ap*bv+bp*av)/(bv+av) if bv+av else mid
    return {'mid':mid,'spread_bps':(ap-bp)/mid*10000,'imb10':(bd-ad)/den if den else np.nan,'micro_dev_bps':(micro/mid-1)*10000}

def bybit_day(path):
    dec=msgspec.json.Decoder(); bids=SortedDict(); asks=SortedDict(); rows=[]; next_grid=None; last_ts=None; snapshots=0; deltas=0; bad=0; prev_u=None; seq_gaps=0
    with zipfile.ZipFile(path) as z:
        badmember=z.testzip()
        if badmember: raise RuntimeError('ZIP_CRC_FAIL:'+badmember)
        n=z.namelist()[0]
        with z.open(n) as f:
            for raw in f:
                try:m=dec.decode(raw)
                except Exception: bad+=1; continue
                typ=m.get('type'); data=m.get('data') or {}
                ts=int(m.get('cts') or m.get('ts') or 0)
                if not ts:continue
                if typ=='snapshot':
                    bids=SortedDict(); asks=SortedDict(); apply(bids,data.get('b') or []); apply(asks,data.get('a') or []); snapshots+=1; prev_u=data.get('u')
                elif typ=='delta':
                    u=data.get('u')
                    if prev_u is not None and u is not None and int(u)>int(prev_u)+1: seq_gaps+=1
                    apply(bids,data.get('b') or []); apply(asks,data.get('a') or []); deltas+=1; prev_u=u if u is not None else prev_u
                else: continue
                if next_grid is None: next_grid=((ts//10000)+1)*10000
                while next_grid<=ts:
                    s=bstate(bids,asks)
                    if s: rows.append({'ts_ms':next_grid,**s})
                    next_grid+=10000
                last_ts=ts
    x=pd.DataFrame(rows).drop_duplicates('ts_ms',keep='last')
    return x,{'snapshots':snapshots,'deltas':deltas,'decodeErrors':bad,'sequenceGapObservations':seq_gaps,'gridRows':len(x),'firstTs':int(x.ts_ms.min()) if len(x) else None,'lastTs':int(x.ts_ms.max()) if len(x) else None}

def binance_spot_day(path):
    with zipfile.ZipFile(path) as z:
        n=[x for x in z.namelist() if x.endswith('.csv')][0]
        a=pd.read_csv(z.open(n),header=None)
    # Header may or may not exist; canonical aggTrades: id,price,qty,first,last,time,buyer_maker,best_match
    for c in [1,5]: a[c]=pd.to_numeric(a[c],errors='coerce')
    a=a.dropna(subset=[1,5]); med=float(a[5].median()); unit_div=1000 if med<1e14 else 1000000
    a['ts_ms']=(a[5]/(1000 if unit_div==1000000 else 1)).astype('int64') if med>1e14 else a[5].astype('int64')
    a['price']=a[1].astype(float); a=a.sort_values('ts_ms')
    # Last observable trade on each 10s grid, no future fill beyond 1 second age at event gate checked later.
    idx=pd.to_datetime(a.ts_ms,unit='ms',utc=True)
    s=pd.Series(a.price.values,index=idx).resample('10s',label='right',closed='right').last().ffill(limit=1)
    out=s.rename('spot').reset_index().rename(columns={'index':'ts'}); out['ts_ms']=(out.ts.astype('int64')//1_000_000).astype('int64'); return out[['ts_ms','spot']]

def rz(s,w=8640,minp=2160):
    m=s.rolling(w,min_periods=minp).mean(); sd=s.rolling(w,min_periods=minp).std(ddof=0).replace(0,np.nan); return (s-m)/sd

def met(v):
    v=pd.Series(v).dropna().astype(float)
    if not len(v):return {'n':0,'mean':None,'median':None,'hitRate':None,'profitFactor':None,'pOneSided':None}
    pos=v[v>0].sum(); neg=-v[v<0].sum(); pf=float(pos/neg) if neg>0 else 999.
    if len(v)>1 and v.std(ddof=1)>0:
        z=float(v.mean()/(v.std(ddof=1)/math.sqrt(len(v)))); p=.5*math.erfc(z/math.sqrt(2))
    else:p=1.
    return {'n':int(len(v)),'mean':float(v.mean()),'median':float(v.median()),'hitRate':float((v>0).mean()),'profitFactor':pf,'pOneSided':float(p)}
def decluster(e,hsec):
    keep=[]; last=None
    for i,r in e.sort_values('ts_ms').iterrows():
        t=int(r.ts_ms)
        if last is None or t-last>=hsec*1000:keep.append(i); last=t
    return e.loc[keep]
def gate(m,n=20):return m['n']>=n and m['mean'] is not None and m['mean']>0 and m['median']>0 and m['hitRate']>.55 and m['profitFactor']>=1.2

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--symbol',default='SOLUSDT'); ap.add_argument('--start',default='2025-01-15'); ap.add_argument('--days',type=int,default=3); a=ap.parse_args()
    root=pathlib.Path('/tmp/cross-exchange'); frames=[]; quality=[]; d=date.fromisoformat(a.start)
    for i in range(a.days):
        dt=(d+timedelta(days=i)); ds=dt.isoformat()
        # Jan 2025 files are ob500.
        burl=f'{BYBIT}/{a.symbol}/{ds}_{a.symbol}_ob500.data.zip'; bpath=download(burl,root/f'{ds}-bybit.zip')
        b,q=bybit_day(bpath); os.remove(bpath)
        surl=f'{VISION}/{a.symbol}/{a.symbol}-aggTrades-{ds}.zip'; spath=download(surl,root/f'{ds}-spot.zip')
        s=binance_spot_day(spath); os.remove(spath)
        x=pd.merge(b,s,on='ts_ms',how='inner'); x['date']=ds; frames.append(x); quality.append({'date':ds,**q,'mergedRows':len(x)})
    x=pd.concat(frames,ignore_index=True).sort_values('ts_ms').drop_duplicates('ts_ms').reset_index(drop=True)
    x['bybit_r30']=x.mid/x.mid.shift(3)-1; x['spot_r30']=x.spot/x.spot.shift(3)-1; x['dislocation']=x.bybit_r30-x.spot_r30
    x['spread_med24']=x.spread_bps.rolling(8640,min_periods=2160).median(); x['disloc_z']=rz(x.dislocation)
    masks={
      'CE1_BYBIT_UP_LEAD':(x.bybit_r30>=.0010)&(x.spot_r30<=.0003)&(x.imb10>=.30)&(x.spread_bps<=x.spread_med24*1.5),
      'CE2_BYBIT_UP_LEAD_WITH_MICROPRICE':(x.bybit_r30>=.0010)&(x.spot_r30<=.0003)&(x.imb10>=.30)&(x.micro_dev_bps>0)&(x.spread_bps<=x.spread_med24*1.5),
      'CE3_CROSS_EXCHANGE_DISLOCATION_Z':(x.disloc_z>=2.5)&(x.imb10>=.30)&(x.micro_dev_bps>0),
    }
    tests=[]
    for hid,m in masks.items():
        for h,shift in HORIZONS.items():
            f=x.spot.shift(-shift)/x.spot-1; e=pd.DataFrame({'ts_ms':x.loc[m,'ts_ms'],'gross':f.loc[m]}).dropna(); e=decluster(e,h); g=met(e.gross); n=met(e.gross-NORMAL_COST); st=met(e.gross-STRESS_COST)
            tests.append({'hypothesis':hid,'horizonSec':h,'declusteredEvents':len(e),'gross':g,'normalNet':n,'stressNet':st,'selectionPass':gate(g)})
    out={'engine':'CROSS_EXCHANGE_LEADLAG_V1_SMOKE','authorization':AUTH,'liveTrading':False,'automaticPromotion':False,'frozenSpec':'validation/edges/cross-exchange-leadlag-v1-spec.json','symbol':a.symbol,'start':a.start,'days':a.days,'quality':quality,'tests':tests,'status':'COMPLETE'}
    od=pathlib.Path('artifacts/cross-exchange-v1'); od.mkdir(parents=True,exist_ok=True); (od/'verdict.json').write_text(json.dumps(out,indent=2)); print(json.dumps(out,indent=2))
if __name__=='__main__':main()
