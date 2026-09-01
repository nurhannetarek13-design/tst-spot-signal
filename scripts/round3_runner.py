import json, math, time, urllib.request, urllib.parse
from pathlib import Path
import numpy as np, pandas as pd

SYMS=['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','TRXUSDT','LINKUSDT','AVAXUSDT','DOTUSDT','LTCUSDT','BCHUSDT','NEARUSDT','UNIUSDT','AAVEUSDT','ETCUSDT','FILUSDT','ATOMUSDT','APTUSDT','ARBUSDT','OPUSDT','INJUSDT','SUIUSDT']
HOLD=['XLMUSDT','HBARUSDT','ICPUSDT','ALGOUSDT','VETUSDT','THETAUSDT','RUNEUSDT','GRTUSDT']
DAYS=420; RAW_MIN=.015; HIT_MIN=.55; RATIO_MIN=2.0
BASE_COST=.0036; CAPITAL=20.12; ORDER=7.; RESERVE=2.; FLOOR=0.
CRASH_Z=-2.5; BREADTH=.60; ASSET_RET=-.03; VOL_Z=2.5
RES_Z=-3.; BETA_LB=24*30; Z_LB=24*60; MINOBS=24*15
BASES=['https://api.binance.com','https://api1.binance.com','https://api2.binance.com','https://data-api.binance.vision']

def get(path,params=None):
    q=('?'+urllib.parse.urlencode(params)) if params else ''
    last=None
    for b in BASES:
        try:
            with urllib.request.urlopen(b+path+q, timeout=25) as r: return json.loads(r.read())
        except Exception as e: last=e
    raise last

def klines(sym):
    out=[]; cur=int((time.time()-DAYS*86400)*1000); now=int(time.time()*1000)
    while cur<now:
        p=get('/api/v3/klines',{'symbol':sym,'interval':'1h','limit':1000,'startTime':cur})
        if not p: break
        for k in p:
            out.append([pd.to_datetime(k[0],unit='ms',utc=True),sym,float(k[1]),float(k[2]),float(k[3]),float(k[4]),float(k[5])])
        nxt=int(p[-1][6])+1
        if nxt<=cur: break
        cur=nxt
        if len(p)<1000: break
    return pd.DataFrame(out,columns=['timestamp','symbol','open','high','low','close','volume'])

def add_basic(df):
    x=df.sort_values(['symbol','timestamp']).copy(); g=x.groupby('symbol',group_keys=False)
    x['ret1']=g.close.pct_change(); x['ret24']=g.close.pct_change(24)
    lv=np.log1p(x.volume); x['_lv']=lv
    mu=x.groupby('symbol')['_lv'].transform(lambda s:s.shift(1).rolling(168,min_periods=72).mean())
    sd=x.groupby('symbol')['_lv'].transform(lambda s:s.shift(1).rolling(168,min_periods=72).std())
    x['vz']=(x['_lv']-mu)/sd.replace(0,np.nan); return x.drop(columns='_lv')

def event_paths(df,col,h):
    z=[]
    for sym,s in df.groupby('symbol'):
        s=s.sort_values('timestamp').reset_index(drop=True); idx=np.flatnonzero(s[col].fillna(False).to_numpy())
        for i in idx:
            if i+1>=len(s) or i+h>=len(s): continue
            e=float(s.loc[i+1,'open']); path=s.loc[i+1:i+h]; ex=float(s.loc[i+h,'close'])
            if e<=0: continue
            r=ex/e-1; mfe=float(path.high.max()/e-1); mae=abs(min(float(path.low.min()/e-1),0.0))
            z.append((s.loc[i,'timestamp'],sym,r,mfe,mae))
    return pd.DataFrame(z,columns=['timestamp','symbol','r','mfe','mae'])

def stats(p):
    if p.empty:return {'n':0,'mean':None,'median':None,'hit':None,'ratio':None,'pass':False}
    m=float(p.r.mean()); med=float(p.r.median()); hit=float((p.r>0).mean()); medmfe=float(p.mfe.median()); medmae=float(p.mae.median()); ratio=(medmfe/medmae if medmae>0 else (999 if medmfe>0 else 0))
    return {'n':len(p),'mean':m,'median':med,'hit':hit,'medianMFE':medmfe,'medianMAE':medmae,'ratio':ratio,'beatCost':float((p.r>BASE_COST).mean()),'beat150bp':float((p.r>RAW_MIN).mean()),'pass':bool(m>=RAW_MIN and med>0 and hit>HIT_MIN and ratio>=RATIO_MIN)}

def crash_events(df):
    x=add_basic(df); m=x.groupby('timestamp').agg(mret=('ret1','median'))
    m['breadth']=x.assign(severe=x.ret1<=ASSET_RET).groupby('timestamp').severe.mean()
    mu=m.mret.shift(1).rolling(720,min_periods=240).mean(); sd=m.mret.shift(1).rolling(720,min_periods=240).std(); m['mz']=(m.mret-mu)/sd
    x=x.merge(m[['breadth','mz']].reset_index(),on='timestamp',how='left'); x['prev']=x.groupby('symbol').ret1.shift(1)
    x['event']=(x.mz<=CRASH_Z)&(x.breadth>=BREADTH)&(x.ret1<=ASSET_RET)&(x.vz>=VOL_Z)&(x.ret1<0)&(x.ret1>x.prev)
    return x

def residual_events(df):
    x=add_basic(df); market=x.groupby('timestamp').ret24.median().rename('mret24'); x=x.merge(market.reset_index(),on='timestamp',how='left')
    outs=[]
    for sym,s in x.groupby('symbol'):
        s=s.sort_values('timestamp').reset_index(drop=True).copy(); a=s.ret24; m=s.mret24
        cov=a.shift(1).rolling(BETA_LB,min_periods=MINOBS).cov(m.shift(1)); var=m.shift(1).rolling(BETA_LB,min_periods=MINOBS).var(); beta=cov/var.replace(0,np.nan)
        resid=a-beta*m; mu=resid.shift(1).rolling(Z_LB,min_periods=MINOBS).mean(); sd=resid.shift(1).rolling(Z_LB,min_periods=MINOBS).std(); rz=(resid-mu)/sd.replace(0,np.nan)
        s['beta']=beta;s['resid']=resid;s['rz']=rz;s['prev_rz']=rz.shift(1);s['event']=(s.prev_rz<RES_Z)&(s.rz>s.prev_rz)&(s.ret1>0)&(s.vz>=2.0);outs.append(s)
    return pd.concat(outs,ignore_index=True)

def simulate(paths):
    eq=CAPITAL;peak=eq;dd=0.;accepted=0
    for r in paths.sort_values('timestamp').itertuples():
        if eq<ORDER+RESERVE: continue
        pnl=ORDER*(r.r-BASE_COST); ne=eq+pnl; accepted+=1
        if ne<=FLOOR: eq=FLOOR;dd=1.;break
        eq=ne;peak=max(peak,eq);dd=max(dd,(peak-eq)/peak)
    return {'endingEquity':eq,'accepted':accepted,'maxDDPct':min(100,dd*100),'ruined':eq<=FLOOR}

def run_block(name,fn,df):
    ev=fn(df); res={}
    for h in (24,48,72):
        p=event_paths(ev,'event',h); st=stats(p); st['capital']=simulate(p) if st['pass'] else None; res[str(h)]=st
    return {'name':name,'horizons':res,'eventCount':int(ev.event.sum())}

print('downloading discovery...')
disc=pd.concat([klines(s) for s in SYMS],ignore_index=True)
print('downloaded',len(disc))
out={'generatedAt':pd.Timestamp.utcnow().isoformat(),'protocol':{'rawMeanMin':RAW_MIN,'medianMin':0,'hitRateMin':HIT_MIN,'medianMfeMaeMin':RATIO_MIN,'costAfterGate':BASE_COST,'equityFloor':FLOOR},'discoverySymbols':SYMS,'results':[run_block('LIQUIDITY_CRASH_EXHAUSTION_OHLCV',crash_events,disc),run_block('RESIDUAL_DISPERSION_REVERSAL',residual_events,disc)]}
passing=[r for r in out['results'] if any(v['pass'] for v in r['horizons'].values())]
out['passingDiscoveryFamilies']=[r['name'] for r in passing]
if passing:
    print('downloading holdout...')
    hdf=pd.concat([klines(s) for s in HOLD],ignore_index=True)
    combo=pd.concat([disc[disc.symbol=='BTCUSDT'],hdf],ignore_index=True)
    out['holdoutSymbols']=HOLD;out['holdoutResults']=[]
    for r in passing:
        fn=crash_events if r['name'].startswith('LIQUIDITY') else residual_events
        out['holdoutResults'].append(run_block(r['name'],fn,combo))
else:
    out['holdoutSymbols']=HOLD;out['holdoutResults']='NOT_RUN_NO_DISCOVERY_EVENT_PASSED_RAW_GATE'
Path('validation').mkdir(exist_ok=True);Path('validation/round3-step2.json').write_text(json.dumps(out,indent=2,default=str));print(json.dumps(out,indent=2,default=str))
