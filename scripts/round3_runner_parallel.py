import json,time,urllib.request,urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
import numpy as np,pandas as pd
SYMS=['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','TRXUSDT','LINKUSDT','AVAXUSDT','DOTUSDT','LTCUSDT','BCHUSDT','NEARUSDT','UNIUSDT','AAVEUSDT','ETCUSDT','FILUSDT','ATOMUSDT','APTUSDT','ARBUSDT','OPUSDT','INJUSDT','SUIUSDT'];HOLD=['XLMUSDT','HBARUSDT','ICPUSDT','ALGOUSDT','VETUSDT','THETAUSDT','RUNEUSDT','GRTUSDT']
DAYS=420;RAW_MIN=.015;HIT_MIN=.55;RATIO_MIN=2.;BASE_COST=.0036;CAPITAL=20.12;ORDER=7.;RESERVE=2.;FLOOR=0.;CRASH_Z=-2.5;BREADTH=.60;ASSET_RET=-.03;VOL_Z=2.5;RES_Z=-3.;BETA_LB=720;Z_LB=1440;MINOBS=360
BASES=['https://api.binance.com','https://api1.binance.com','https://api2.binance.com','https://data-api.binance.vision']
def get(path,params):
 q='?'+urllib.parse.urlencode(params);last=None
 for b in BASES:
  try:
   req=urllib.request.Request(b+path+q,headers={'User-Agent':'round3-research/1.0'})
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
def download(syms):
 d={}
 with ThreadPoolExecutor(max_workers=8) as ex:
  fut={ex.submit(klines,s):s for s in syms}
  for f in as_completed(fut):
   s=fut[f];d[s]=f.result();print('downloaded',s,len(d[s]),flush=True)
 return pd.concat([d[s] for s in syms],ignore_index=True)
def basic(df):
 x=df.sort_values(['symbol','timestamp']).copy();g=x.groupby('symbol',group_keys=False);x['ret1']=g.close.pct_change();x['ret24']=g.close.pct_change(24);x['_lv']=np.log1p(x.volume);mu=x.groupby('symbol')['_lv'].transform(lambda s:s.shift().rolling(168,min_periods=72).mean());sd=x.groupby('symbol')['_lv'].transform(lambda s:s.shift().rolling(168,min_periods=72).std());x['vz']=(x._lv-mu)/sd.replace(0,np.nan);return x.drop(columns='_lv')
def paths(df,h):
 z=[]
 for sym,s in df.groupby('symbol'):
  s=s.sort_values('timestamp').reset_index(drop=True)
  for i in np.flatnonzero(s.event.fillna(False).to_numpy()):
   if i+1>=len(s) or i+h>=len(s):continue
   e=float(s.loc[i+1,'open']);q=s.loc[i+1:i+h];ex=float(s.loc[i+h,'close']);r=ex/e-1;mfe=float(q.high.max()/e-1);mae=abs(min(float(q.low.min()/e-1),0));z.append((s.loc[i,'timestamp'],sym,r,mfe,mae))
 return pd.DataFrame(z,columns=['timestamp','symbol','r','mfe','mae'])
def st(p):
 if p.empty:return {'n':0,'pass':False}
 mean=float(p.r.mean());med=float(p.r.median());hit=float((p.r>0).mean());mfe=float(p.mfe.median());mae=float(p.mae.median());ratio=mfe/mae if mae>0 else 999
 return {'n':len(p),'mean':mean,'median':med,'hit':hit,'medianMFE':mfe,'medianMAE':mae,'ratio':ratio,'beatCost':float((p.r>BASE_COST).mean()),'beat150bp':float((p.r>RAW_MIN).mean()),'pass':bool(mean>=RAW_MIN and med>0 and hit>HIT_MIN and ratio>=RATIO_MIN)}
def crash(df):
 x=basic(df);m=x.groupby('timestamp').ret1.median().rename('mr').to_frame();m['breadth']=x.assign(sev=x.ret1<=ASSET_RET).groupby('timestamp').sev.mean();mu=m.mr.shift().rolling(720,min_periods=240).mean();sd=m.mr.shift().rolling(720,min_periods=240).std();m['mz']=(m.mr-mu)/sd;x=x.merge(m[['breadth','mz']].reset_index(),on='timestamp');x['prev']=x.groupby('symbol').ret1.shift();x['event']=(x.mz<=CRASH_Z)&(x.breadth>=BREADTH)&(x.ret1<=ASSET_RET)&(x.vz>=VOL_Z)&(x.ret1<0)&(x.ret1>x.prev);return x
def residual(df):
 x=basic(df);m=x.groupby('timestamp').ret24.median().rename('mret24');x=x.merge(m.reset_index(),on='timestamp');o=[]
 for sym,s in x.groupby('symbol'):
  s=s.sort_values('timestamp').reset_index(drop=True).copy();a=s.ret24;m=s.mret24;cov=a.shift().rolling(BETA_LB,min_periods=MINOBS).cov(m.shift());var=m.shift().rolling(BETA_LB,min_periods=MINOBS).var();b=cov/var.replace(0,np.nan);res=a-b*m;mu=res.shift().rolling(Z_LB,min_periods=MINOBS).mean();sd=res.shift().rolling(Z_LB,min_periods=MINOBS).std();rz=(res-mu)/sd.replace(0,np.nan);s['rz']=rz;s['prz']=rz.shift();s['event']=(s.prz<RES_Z)&(s.rz>s.prz)&(s.ret1>0)&(s.vz>=2);o.append(s)
 return pd.concat(o,ignore_index=True)
def sim(p):
 eq=CAPITAL;peak=eq;dd=0;accepted=0
 for r in p.sort_values('timestamp').itertuples():
  if eq<ORDER+RESERVE:continue
  ne=eq+ORDER*(r.r-BASE_COST);accepted+=1
  if ne<=FLOOR:eq=0;dd=1;break
  eq=ne;peak=max(peak,eq);dd=max(dd,(peak-eq)/peak)
 return {'endingEquity':eq,'accepted':accepted,'maxDDPct':min(100,100*dd),'ruined':eq<=0}
def block(name,fn,df):
 ev=fn(df);hs={}
 for h in [24,48,72]:
  p=paths(ev,h);q=st(p);q['capital']=sim(p) if q['pass'] else None;hs[str(h)]=q
 return {'name':name,'eventCount':int(ev.event.sum()),'horizons':hs}
disc=download(SYMS);results=[block('LIQUIDITY_CRASH_EXHAUSTION_OHLCV',crash,disc),block('RESIDUAL_DISPERSION_REVERSAL',residual,disc)];passing=[r for r in results if any(v.get('pass') for v in r['horizons'].values())];out={'generatedAt':pd.Timestamp.utcnow().isoformat(),'protocol':{'rawMeanMin':RAW_MIN,'medianMin':0,'hitRateMin':HIT_MIN,'medianMfeMaeMin':RATIO_MIN,'costAfterGate':BASE_COST,'equityFloor':FLOOR},'discoverySymbols':SYMS,'results':results,'passingDiscoveryFamilies':[r['name'] for r in passing],'holdoutSymbols':HOLD}
if passing:
 hdf=download(HOLD);combo=pd.concat([disc[disc.symbol=='BTCUSDT'],hdf],ignore_index=True);out['holdoutResults']=[block(r['name'],crash if r['name'].startswith('LIQUIDITY') else residual,combo) for r in passing]
else:out['holdoutResults']='NOT_RUN_NO_DISCOVERY_EVENT_PASSED_RAW_GATE'
Path('validation').mkdir(exist_ok=True);Path('validation/round3-step2-parallel.json').write_text(json.dumps(out,indent=2,default=str));print(json.dumps(out,indent=2,default=str))
