#!/usr/bin/env python3
from __future__ import annotations
import json,time,urllib.parse,urllib.request,pathlib
import pandas as pd,numpy as np
BASE='https://data-api.binance.vision'; OUT=pathlib.Path('validation/pro_stack/feature_store.json')
SYMS=['BTCUSDT','ETHUSDT','SOLUSDT']
def api(p):
 r=urllib.request.Request(BASE+p,headers={'User-Agent':'tst-pro-stack/1.0'});return json.load(urllib.request.urlopen(r,timeout=30))
def k(sym,limit=500):
 q=urllib.parse.urlencode({'symbol':sym,'interval':'15m','limit':limit});b=api('/api/v3/klines?'+q)
 c=['ot','open','high','low','close','volume','ct','qv','trades','tb','tq','ignore'];d=pd.DataFrame(b,columns=c)
 for x in ['open','high','low','close','volume','qv','tq']:d[x]=pd.to_numeric(d[x],errors='coerce')
 return d
def latest_features(sym,d,btc):
 flow=d.tq/d.qv.replace(0,np.nan); ret1=d.close.pct_change(1); ret4=d.close.pct_change(4); ret16=d.close.pct_change(16)
 volz=(d.qv-d.qv.rolling(96).mean())/d.qv.rolling(96).std(); flowz=(flow-flow.rolling(96).mean())/flow.rolling(96).std()
 rv=ret1.rolling(16).std()*np.sqrt(16)
 btc_r4=btc.close.pct_change(4); rel=ret4-btc_r4
 i=-1
 return {'symbol':sym,'price':float(d.close.iloc[i]),'ret15m':float(ret1.iloc[i]),'ret1h':float(ret4.iloc[i]),'ret4h':float(ret16.iloc[i]),'quoteVolumeZ':float(volz.iloc[i]),'takerBuyShare':float(flow.iloc[i]),'flowZ':float(flowz.iloc[i]),'realizedVol1h':float(rv.iloc[i]),'relativeStrength1hVsBTC':float(rel.iloc[i]),'btcRet1h':float(btc_r4.iloc[i]),'ts':int(d.ot.iloc[i])}
def main():
 data={s:k(s) for s in SYMS}; btc=data['BTCUSDT']; rows=[latest_features(s,data[s],btc) for s in SYMS]
 out={'engine':'PRO_STACK_FEATURE_STORE_V1','authorization':'RESEARCH_ONLY','liveTrading':False,'rows':rows};OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(out,indent=2));print(json.dumps(out))
if __name__=='__main__':main()
