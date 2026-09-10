#!/usr/bin/env python3
from __future__ import annotations
import json,time,urllib.parse,urllib.request,pathlib
import pandas as pd,numpy as np
BASE='https://data-api.binance.vision'; OUT=pathlib.Path('validation/pro_stack/feature_store.json')
SYMS=['BTCUSDT','ETHUSDT','SOLUSDT']
def api(p):
 r=urllib.request.Request(BASE+p,headers={'User-Agent':'tst-pro-stack/1.1'});return json.load(urllib.request.urlopen(r,timeout=30))
def k(sym,limit=500):
 q=urllib.parse.urlencode({'symbol':sym,'interval':'15m','limit':limit});b=api('/api/v3/klines?'+q)
 c=['ot','open','high','low','close','volume','ct','qv','trades','tb','tq','ignore'];d=pd.DataFrame(b,columns=c)
 for x in ['open','high','low','close','volume','qv','tq']:d[x]=pd.to_numeric(d[x],errors='coerce')
 d['ot']=pd.to_numeric(d.ot,errors='coerce');d['ct']=pd.to_numeric(d.ct,errors='coerce');return d.dropna()
def latest_closed_index(d,now_ms):
 ix=np.flatnonzero(d.ct.to_numpy(dtype=float)<now_ms)
 if not len(ix):raise RuntimeError('NO_CLOSED_15M_CANDLE')
 return int(ix[-1])
def latest_features(sym,d,btc,now_ms):
 flow=d.tq/d.qv.replace(0,np.nan);ret1=d.close.pct_change(1);ret4=d.close.pct_change(4);ret16=d.close.pct_change(16)
 volz=(d.qv-d.qv.rolling(96).mean())/d.qv.rolling(96).std();flowz=(flow-flow.rolling(96).mean())/flow.rolling(96).std();rv=ret1.rolling(16).std()*np.sqrt(16);btc_r4=btc.close.pct_change(4);rel=ret4-btc_r4
 i=latest_closed_index(d,now_ms); vals=[ret1.iloc[i],ret4.iloc[i],ret16.iloc[i],volz.iloc[i],flow.iloc[i],flowz.iloc[i],rv.iloc[i],rel.iloc[i],btc_r4.iloc[i]]
 if not all(np.isfinite(float(v)) for v in vals):raise RuntimeError(f'NONFINITE_FEATURES:{sym}')
 return {'symbol':sym,'price':float(d.close.iloc[i]),'ret15m':float(ret1.iloc[i]),'ret1h':float(ret4.iloc[i]),'ret4h':float(ret16.iloc[i]),'quoteVolumeZ':float(volz.iloc[i]),'takerBuyShare':float(flow.iloc[i]),'flowZ':float(flowz.iloc[i]),'realizedVol1h':float(rv.iloc[i]),'relativeStrength1hVsBTC':float(rel.iloc[i]),'btcRet1h':float(btc_r4.iloc[i]),'ts':int(d.ot.iloc[i]),'candleCloseTs':int(d.ct.iloc[i]),'candleClosed':True}
def main():
 now_ms=int(time.time()*1000);data={s:k(s) for s in SYMS};btc=data['BTCUSDT'];rows=[latest_features(s,data[s],btc,now_ms) for s in SYMS]
 ts={r['ts'] for r in rows}
 if len(ts)!=1:raise RuntimeError(f'MISALIGNED_CLOSED_CANDLES:{sorted(ts)}')
 out={'engine':'PRO_STACK_FEATURE_STORE_V2_CLOSED_CANDLES','authorization':'RESEARCH_ONLY','liveTrading':False,'asOfMs':now_ms,'rows':rows};OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(out,indent=2));print(json.dumps(out))
if __name__=='__main__':main()
