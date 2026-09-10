#!/usr/bin/env python3
"""Historical replay of frozen Gate A Spot baseline. DIAGNOSTIC ONLY.
Never contributes rows to the forward Gate A sample and never authorizes live trading.
"""
from __future__ import annotations
import argparse,json,math,pathlib,time,urllib.parse,urllib.request
from datetime import datetime,timezone,timedelta
import numpy as np,pandas as pd
BASE='https://data-api.binance.vision'; COST=.0028; TP=.012; SL=.007; H=16
OUT=pathlib.Path('validation/pro_stack/gate-a-historical-replay-v1.json')
def api(path):
 req=urllib.request.Request(BASE+path,headers={'User-Agent':'tst-gate-a-replay/1.0'});
 with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
def load(sym,days):
 end=int(time.time()*1000);cur=end-days*86400000;rows=[]
 while cur<end:
  q=urllib.parse.urlencode({'symbol':sym,'interval':'15m','limit':1000,'startTime':cur,'endTime':end});b=api('/api/v3/klines?'+q)
  if not b:break
  rows+=b;nxt=int(b[-1][0])+900000
  if nxt<=cur:break
  cur=nxt
 c=['ot','open','high','low','close','volume','ct','qv','trades','tb','tq','ignore'];d=pd.DataFrame(rows,columns=c).drop_duplicates('ot')
 for x in ['open','high','low','close','volume','qv','tq']:d[x]=pd.to_numeric(d[x],errors='coerce')
 d['ts']=pd.to_datetime(pd.to_numeric(d.ot),unit='ms',utc=True);return d.set_index('ts').sort_index()
def features(d,btc):
 f=pd.DataFrame(index=d.index);flow=d.tq/d.qv.replace(0,np.nan);r1=d.close.pct_change(1);r4=d.close.pct_change(4);volz=(d.qv-d.qv.rolling(96).mean())/d.qv.rolling(96).std();flowz=(flow-flow.rolling(96).mean())/flow.rolling(96).std();rv=r1.rolling(16).std()*np.sqrt(16);br4=btc.close.pct_change(4).reindex(d.index);rel=r4-br4
 z=.7*flowz+.45*volz+8*r4+4*rel+3*br4-2.5*rv;p=1/(1+np.exp(-z));gross=(p-.5)*.03;edge=gross-COST
 return pd.DataFrame({'p':p,'expectedEdge':edge,'close':d.close,'high':d.high,'low':d.low},index=d.index)
def replay(sym,f):
 rows=[];i=96
 while i<len(f)-H:
  r=f.iloc[i]
  if not (np.isfinite(r.p) and r.p>=.65 and r.expectedEdge>0):i+=1;continue
  e=float(r.close);reason='TIMEOUT';px=float(f.close.iloc[i+H]);hold=H*15;mfe=-1e9;mae=1e9;exit_i=i+H
  for j in range(i+1,i+H+1):
   hi=float(f.high.iloc[j]);lo=float(f.low.iloc[j]);mfe=max(mfe,hi/e-1);mae=min(mae,lo/e-1)
   if lo<=e*(1-SL):reason='SL';px=e*(1-SL);hold=(j-i)*15;exit_i=j;break
   if hi>=e*(1+TP):reason='TP';px=e*(1+TP);hold=(j-i)*15;exit_i=j;break
  net=px/e-1-COST;rows.append({'symbol':sym,'entryTs':f.index[i].isoformat(),'exitTs':f.index[exit_i].isoformat(),'p':float(r.p),'expectedEdgePct':float(r.expectedEdge*100),'reason':reason,'netPct':float(net*100),'mfePct':float(mfe*100),'maePct':float(mae*100),'holdingMin':hold});i=exit_i+1
 return rows
def metrics(rows):
 v=np.array([r['netPct'] for r in rows],float)
 if not len(v):return {'n':0,'profitFactor':0,'hitRate':0,'meanNetPct':0,'maxDrawdownPct':0}
 w=v[v>0].sum();l=-v[v<0].sum();eq=np.cumsum(v);peak=np.maximum.accumulate(np.r_[0,eq])[1:];dd=peak-eq
 return {'n':len(v),'profitFactor':float(w/l) if l>0 else (99 if w>0 else 0),'hitRate':float((v>0).mean()),'meanNetPct':float(v.mean()),'medianNetPct':float(np.median(v)),'totalNetPct':float(v.sum()),'maxDrawdownPct':float(dd.max() if len(dd) else 0)}
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--days',type=int,default=180);a=ap.parse_args();data={s:load(s,a.days) for s in ['BTCUSDT','ETHUSDT','SOLUSDT']};btc=data['BTCUSDT'];tr=[]
 for s in ['ETHUSDT','SOLUSDT']:tr+=replay(s,features(data[s],btc))
 tr=sorted(tr,key=lambda x:x['entryTs']);m=metrics(tr);out={'engine':'GATE_A_HISTORICAL_REPLAY_V1','authorization':'RESEARCH_ONLY','liveTrading':False,'countsTowardForwardGateA':False,'experimentReference':'SPOT_BASELINE_GATE_A_V1','days':a.days,'symbols':['ETHUSDT','SOLUSDT'],'barrier':{'tpPct':1.2,'slPct':0.7,'timeoutMin':240,'sameBar':'SL_WINS'},'roundTripCostPct':.28,'metrics':m,'bySymbol':{s:metrics([r for r in tr if r['symbol']==s]) for s in ['ETHUSDT','SOLUSDT']},'trades':tr[-100:]};OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(out,indent=2));print(json.dumps({'metrics':m,'bySymbol':out['bySymbol']}))
if __name__=='__main__':main()
