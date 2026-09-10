#!/usr/bin/env python3
from __future__ import annotations
import json,pathlib,time,urllib.parse,urllib.request
from concurrent.futures import ThreadPoolExecutor,as_completed
import numpy as np,pandas as pd
BASE='https://data-api.binance.vision';DAYS=365;MAX_SYMBOLS=40;MIN_QV=20_000_000;HORIZONS=(6,12,24,48);COST=.0028;STRESS=.005;MIN_N=30;OUT=pathlib.Path('validation/edges/current-data-candidate-discovery-v1.json');EX={'USDC','FDUSD','TUSD','USDP','DAI','BUSD','EUR','AEUR','TRY','BRL','GBP','AUD','USD1','RLUSD','USDE','PAXG','XAUT','U'}
def api(p):
 r=urllib.request.Request(BASE+p,headers={'User-Agent':'tst-current-edge/1.0'});return json.load(urllib.request.urlopen(r,timeout=30))
def universe():
 info=api('/api/v3/exchangeInfo');tick={x['symbol']:x for x in api('/api/v3/ticker/24hr')};a=[]
 for s in info['symbols']:
  b=s.get('baseAsset','');sym=s.get('symbol','');q=float(tick.get(sym,{}).get('quoteVolume') or 0)
  if s.get('status')=='TRADING' and s.get('quoteAsset')=='USDT' and s.get('isSpotTradingAllowed') and b not in EX and not b.endswith(('UP','DOWN','BULL','BEAR')) and q>=MIN_QV:a.append((sym,q))
 return [s for s,_ in sorted(a,key=lambda x:x[1],reverse=True)[:MAX_SYMBOLS]]
def load(sym):
 end=int(time.time()*1000);cur=end-DAYS*86400000;rows=[]
 while cur<end:
  q=urllib.parse.urlencode({'symbol':sym,'interval':'1h','limit':1000,'startTime':cur,'endTime':end});b=api('/api/v3/klines?'+q)
  if not b:break
  rows+=b;nxt=int(b[-1][0])+3600000
  if nxt<=cur:break
  cur=nxt
 c=['ot','open','high','low','close','volume','ct','qv','trades','tb','tq','ignore'];d=pd.DataFrame(rows,columns=c)
 for x in ['open','high','low','close','volume','qv','tq']:d[x]=pd.to_numeric(d[x],errors='coerce')
 return d.dropna().reset_index(drop=True)
def feat(d):
 f=pd.DataFrame(index=d.index);f['r6']=d.close.pct_change(6);f['volz']=(d.qv-d.qv.rolling(168).mean())/d.qv.rolling(168).std();flow=d.tq/d.qv.replace(0,np.nan);f['flowz']=(flow-flow.rolling(168).mean())/flow.rolling(168).std();f['breakout']=d.close/d.high.rolling(168).max().shift(1)-1;return f
def mask(f,n):
 return {'FLOW_EXHAUSTION':(f.flowz<=-2)&(f.r6<=-.02),'FLOW_MOMENTUM':(f.flowz>=2)&(f.r6>=.015),'VOLUME_REVERSAL':(f.volz>=3)&(f.r6<=-.025),'BREAKOUT_FLOW':(f.breakout>0)&(f.flowz>=1)&(f.volz>=1.5)}[n].fillna(False).to_numpy()
def stats(d,ix,h,c):
 a=np.array([float(d.close.iloc[i+h]/d.close.iloc[i]-1-c) for i in ix if i+h<len(d)])
 if not len(a):return {'n':0}
 loss=-a[a<0].sum();return {'n':len(a),'mean':float(a.mean()),'median':float(np.median(a)),'hitRate':float((a>0).mean()),'profitFactor':float(a[a>0].sum()/loss) if loss>0 else 99.}
def ok(s,stress=False):return s.get('n',0)>=MIN_N and s.get('mean',-1)>0 and s.get('median',-1)>0 and s.get('hitRate',0)>=.55 and s.get('profitFactor',0)>=(1.15 if stress else 1.25)
def main():
 syms=universe();data={};fail={}
 with ThreadPoolExecutor(max_workers=8) as ex:
  fs={ex.submit(load,s):s for s in syms}
  for z in as_completed(fs):
   s=fs[z]
   try:data[s]=z.result()
   except Exception as e:fail[s]=str(e)
 fams=['FLOW_EXHAUSTION','FLOW_MOMENTUM','VOLUME_REVERSAL','BREAKOUT_FLOW'];rows=[]
 for s,d in data.items():
  f=feat(d);n=len(d);a=int(n*.6);b=int(n*.8)
  for fam in fams:
   m=mask(f,fam)
   for h in HORIZONS:
    ds=stats(d,np.flatnonzero(m[:a]),h,COST);vs=stats(d,np.flatnonzero(m[a:b])+a,h,COST);os=stats(d,np.flatnonzero(m[b:])+b,h,COST);ss=stats(d,np.flatnonzero(m[b:])+b,h,STRESS);rows.append({'symbol':s,'family':fam,'horizonHours':h,'discovery':ds,'validation':vs,'oos':os,'stressOos':ss,'pass':ok(ds) and ok(vs) and ok(os) and ok(ss,True)})
 surv=sorted([r for r in rows if r['pass']],key=lambda r:-r['stressOos']['profitFactor']);o={'engine':'CURRENT_DATA_CANDIDATE_DISCOVERY_V1','authorization':'RESEARCH_ONLY','liveTrading':False,'automaticPromotion':False,'costRoundTrip':COST,'stressRoundTrip':STRESS,'symbols':syms,'loaded':list(data),'failures':fail,'frozenFamilies':fams,'tests':len(rows),'survivorCount':len(surv),'survivors':surv,'productionCandidate':False};OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(o,indent=2));print(json.dumps({'loaded':len(data),'tests':len(rows),'survivors':len(surv)}))
if __name__=='__main__':main()
