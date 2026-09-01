import json,time,urllib.request,urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
import numpy as np,pandas as pd
SYMS=['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','TRXUSDT','LINKUSDT','AVAXUSDT','DOTUSDT','LTCUSDT','BCHUSDT','NEARUSDT','UNIUSDT','AAVEUSDT','ETCUSDT','FILUSDT','ATOMUSDT','APTUSDT','ARBUSDT','OPUSDT','INJUSDT','SUIUSDT']
DAYS=420;RAW=.015;HIT=.55;RATIO=2.;BASE_COST=.0036;CAPITAL=20.12;ORDER=7.;RESERVE=2.
BASES=['https://api.binance.com','https://api1.binance.com','https://api2.binance.com','https://data-api.binance.vision']
LEADERS={'BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT'}
def get(path,params):
 q='?'+urllib.parse.urlencode(params);last=None
 for b in BASES:
  try:
   req=urllib.request.Request(b+path+q,headers={'User-Agent':'round3-breadth/1.0'})
   with urllib.request.urlopen(req,timeout=20) as r:return json.loads(r.read())
  except Exception as e:last=e
 raise last
def klines(sym):
 out=[];cur=int((time.time()-DAYS*86400)*1000);now=int(time.time()*1000)
 while cur<now:
  p=get('/api/v3/klines',{'symbol':sym,'interval':'1h','limit':1000,'startTime':cur})
  if not p:break
  out.extend([[pd.to_datetime(k[0],unit='ms',utc=True),sym,*map(float,[k[1],k[2],k[3],k[4],k[5]])] for k in p]);n=int(p[-1][6])+1
  if n<=cur:break
  cur=n
  if len(p)<1000:break
 return pd.DataFrame(out,columns=['timestamp','symbol','open','high','low','close','volume'])
def download():
 d={}
 with ThreadPoolExecutor(max_workers=8) as ex:
  fs={ex.submit(klines,s):s for s in SYMS}
  for f in as_completed(fs):d[fs[f]]=f.result();print('downloaded',fs[f],len(d[fs[f]]),flush=True)
 return pd.concat([d[s] for s in SYMS],ignore_index=True)
def build(df):
 x=df.sort_values(['symbol','timestamp']).copy();g=x.groupby('symbol',group_keys=False);x['ret24']=g.close.pct_change(24);x['sma50d']=g.close.transform(lambda s:s.rolling(24*50,min_periods=24*30).mean());x['above50d']=x.close>x.sma50d
 b=x.groupby('timestamp').above50d.mean().rename('breadth50d');bimp=(b-b.shift(24)).rename('breadthImpulse24h')
 lead=x[x.symbol.isin(LEADERS)].groupby('timestamp').ret24.mean().rename('leader24h');lmu=lead.shift().rolling(24*60,min_periods=24*20).mean();lsd=lead.shift().rolling(24*60,min_periods=24*20).std();lz=((lead-lmu)/lsd.replace(0,np.nan)).rename('leaderZ')
 x=x.merge(pd.concat([b,bimp,lead,lz],axis=1).reset_index(),on='timestamp',how='left')
 # cross-sectional relative-strength percentile at t; uses only current/past data.
 x['rsPct']=x.groupby('timestamp').ret24.rank(pct=True)
 # Frozen macro continuation event definition.
 x['event']=(x.breadth50d>=.60)&(x.breadthImpulse24h>=.10)&(x.leader24h>=.02)&(x.leaderZ>=1.5)&(x.rsPct>=.80)&(x.ret24>0)
 return x
def paths(df,h):
 z=[]
 for sym,s in df.groupby('symbol'):
  s=s.sort_values('timestamp').reset_index(drop=True)
  for i in np.flatnonzero(s.event.fillna(False).to_numpy()):
   if i+1>=len(s) or i+h>=len(s):continue
   e=float(s.loc[i+1,'open']);p=s.loc[i+1:i+h];ex=float(s.loc[i+h,'close']);r=ex/e-1;mfe=float(p.high.max()/e-1);mae=abs(min(float(p.low.min()/e-1),0));z.append((s.loc[i,'timestamp'],sym,r,mfe,mae))
 return pd.DataFrame(z,columns=['timestamp','symbol','r','mfe','mae'])
def stat(p):
 if p.empty:return {'n':0,'pass':False}
 m=float(p.r.mean());med=float(p.r.median());hit=float((p.r>0).mean());mfe=float(p.mfe.median());mae=float(p.mae.median());ratio=mfe/mae if mae>0 else 999
 return {'n':len(p),'mean':m,'median':med,'hit':hit,'medianMFE':mfe,'medianMAE':mae,'ratio':ratio,'beatCost':float((p.r>BASE_COST).mean()),'beat150bp':float((p.r>RAW).mean()),'pass':bool(m>=RAW and med>0 and hit>HIT and ratio>=RATIO)}
def sim(p):
 eq=CAPITAL;peak=eq;dd=0;accepted=0
 for r in p.sort_values('timestamp').itertuples():
  if eq<ORDER+RESERVE:continue
  ne=eq+ORDER*(r.r-BASE_COST);accepted+=1
  if ne<=0:eq=0;dd=1;break
  eq=ne;peak=max(peak,eq);dd=max(dd,(peak-eq)/peak)
 return {'endingEquity':eq,'accepted':accepted,'maxDDPct':min(100,100*dd),'ruined':eq<=0}
df=build(download());res={}
for h in [48,72,120,168]:
 p=paths(df,h);s=stat(p);s['capital']=sim(p) if s['pass'] else None;res[str(h)]=s
out={'generatedAt':pd.Timestamp.utcnow().isoformat(),'definition':{'breadth50dMin':.60,'breadthImpulse24hMin':.10,'leader24hMin':.02,'leaderZMin':1.5,'assetRsPercentileMin':.80},'eventCount':int(df.event.sum()),'horizons':res}
Path('validation').mkdir(exist_ok=True);Path('validation/round3-breadth.json').write_text(json.dumps(out,indent=2,default=str));print(json.dumps(out,indent=2,default=str))
