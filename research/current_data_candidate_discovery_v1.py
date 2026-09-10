#!/usr/bin/env python3
"""Current-data Spot candidate discovery, research only.

Uses Binance public 1h Spot candles and taker-buy quote volume. Definitions are
fixed in code before results. Chronological split: first 60% discovery, next 20%
validation, final 20% OOS. Costs are charged to forward returns. No live trading.
"""
from __future__ import annotations
import json, math, pathlib, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np, pandas as pd
BASE='https://data-api.binance.vision'; DAYS=365; MAX_SYMBOLS=40; MIN_QV=20_000_000
HORIZONS=(6,12,24,48); COST=0.0028; STRESS=0.005; MIN_N=30; WORKERS=8
OUT=pathlib.Path('validation/edges/current-data-candidate-discovery-v1.json')
EX={'USDC','FDUSD','TUSD','USDP','DAI','BUSD','EUR','AEUR','TRY','BRL','GBP','AUD','USD1','RLUSD','USDE','PAXG','XAUT','U'}
def api(path):
  req=urllib.request.Request(BASE+path,headers={'User-Agent':'tst-current-edge/1.0'})
  with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
def universe():
  info=api('/api/v3/exchangeInfo'); tick={x['symbol']:x for x in api('/api/v3/ticker/24hr')}; a=[]
  for s in info['symbols']:
    b=s.get('baseAsset',''); sym=s.get('symbol','')
    if s.get('status')!='TRADING' or s.get('quoteAsset')!='USDT' or not s.get('isSpotTradingAllowed'):continue
    if not b or b in EX or b.endswith(('UP','DOWN','BULL','BEAR')):continue
    q=float(tick.get(sym,{}).get('quoteVolume') or 0)
    if q>=MIN_QV:a.append((sym,q))
  return [s for s,_ in sorted(a,key=lambda x:x[1],reverse=True)[:MAX_SYMBOLS]]
def load(sym):
  end=int(time.time()*1000); cur=end-DAYS*86400000; rows=[]
  while cur<end:
    q=urllib.parse.urlencode({'symbol':sym,'interval':'1h','limit':1000,'startTime':cur,'endTime':end})
    b=api('/api/v3/klines?'+q)
    if not b:break
    rows+=b; nxt=int(b[-1][0])+3600000
    if nxt<=cur:break
    cur=nxt
  c=['ot','open','high','low','close','volume','ct','qv','trades','tb','tq','ignore']; d=pd.DataFrame(rows,columns=c)
  for x in ['open','high','low','close','volume','qv','tq']:d[x]=pd.to_numeric(d[x],errors='coerce')
  d['ts']=pd.to_datetime(d.ot,unit='ms',utc=True); return d.set_index('ts').dropna()
def feats(d):
  x=pd.DataFrame(index=d.index); x['r6']=d.close.pct_change(6); x['r24']=d.close.pct_change(24)
  x['volz']=(d.qv-d.qv.rolling(168).mean())/d.qv.rolling(168).std()
  x['flow']=d.tq/d.qv.replace(0,np.nan); x['flowz']=(x.flow-x.flow.rolling(168).mean())/x.flow.rolling(168).std()
  hi=d.high.rolling(168).max().shift(1); x['breakout']=d.close/hi-1
  return x
def event_mask(f,name):
  if name=='FLOW_EXHAUSTION':return (f.flowz<=-2)&(f.r6<=-0.02)
  if name=='FLOW_MOMENTUM':return (f.flowz>=2)&(f.r6>=0.015)
  if name=='VOLUME_REVERSAL':return (f.volz>=3)&(f.r6<=-0.025)
  if name=='BREAKOUT_FLOW':return (f.breakout>0)&(f.flowz>=1)&(f.volz>=1.5)
  raise KeyError(name)
def stats(d,idx,h,cost):
  vals=[]
  for i in idx:
    if i+h>=len(d):continue
    vals.append(float(d.close.iloc[i+h]/d.close.iloc[i]-1-cost))
  if not vals:return {'n':0}
  a=np.array(vals); win=a[a>0].sum(); loss=-a[a<0].sum(); pf=float(win/loss) if loss>0 else 99.0
  return {'n':len(a),'mean':float(a.mean()),'median':float(np.median(a)),'hitRate':float((a>0).mean()),'profitFactor':pf}
def passes(s,stress=False):
  return s.get('n',0)>=MIN_N and s.get('mean',-1)>0 and s.get('median',-1)>0 and s.get('hitRate',0)>=0.55 and s.get('profitFactor',0)>= (1.15 if stress else 1.25)
def main():
  syms=universe(); data={}; fail={}
  with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    fs={ex.submit(load,s):s for s in syms}
    for f in as_completed(fs):
      s=fs[f]
      try:data[s]=f.result()
      except Exception as e:fail[s]=str(e)
  families=['FLOW_EXHAUSTION','FLOW_MOMENTUM','VOLUME_REVERSAL','BREAKOUT_FLOW']; rows=[]
  for s,d in data.items():
    f=feats(d); n=len(d); cuts=[0,int(n*.6),int(n*.8),n]
    for fam in families:
      m=event_mask(f,fam).fillna(False).to_numpy()
      for h in HORIZONS:
        seg=[]
        for a,b in zip(cuts[1:-1],cuts[2:]):
          ix=np.flatnonzero(m[a:b])+a; seg.append(stats(d,ix,h,COST))
        disc=stats(d,np.flatnonzero(m[cuts[0]:cuts[1]])+cuts[0],h,COST)
        val,oos=seg; stress=stats(d,np.flatnonzero(m[cuts[2]:cuts[3]])+cuts[2],h,STRESS)
        ok=passes(disc) and passes(val) and passes(oos) and passes(stress,True)
        rows.append({'symbol':s,'family':fam,'horizonHours':h,'discovery':disc,'validation':val,'oos':oos,'stressOos':stress,'pass':ok})
  survivors=sorted([r for r in rows if r['pass']],key=lambda r:(-r['stressOos']['profitFactor'],-r['oos']['mean']))
  out={'engine':'CURRENT_DATA_CANDIDATE_DISCOVERY_V1','authorization':'RESEARCH_ONLY','liveTrading':False,'automaticPromotion':False,'days':DAYS,'costRoundTrip':COST,'stressRoundTrip':STRESS,'symbols':syms,'loaded':list(data),'failures':fail,'frozenFamilies':families,'tests':len(rows),'survivorCount':len(survivors),'survivors':survivors,'productionCandidate':False,'nextGate':'Any survivor requires archive-universe survivorship audit and forward shadow before production review.'}
  OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2,sort_keys=True)); print(json.dumps({'engine':out['engine'],'loaded':len(data),'tests':len(rows),'survivors':len(survivors)}))
if __name__=='__main__':main()
