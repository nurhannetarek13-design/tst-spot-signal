#!/usr/bin/env python3
"""Liquidity Sweep Factory V2 — research-only Binance Spot long system.

Idea under test:
  sell-side liquidity sweep -> reclaim -> bullish CHoCH -> participation confirm
  while 1H/4H trend is bullish, optionally requiring bullish BTC regime.

Research contract:
- Public Binance Vision 15m Spot data only; no API keys/order endpoints.
- Next-bar-open entries/exits for close-confirmed signals.
- Protective stop/target may trigger intrabar; if both touch, stop wins.
- 0.10% fee/side + 0.05% slippage/side; OOS stress doubles slippage.
- Chronological Discovery 60% -> Validation 20% -> frozen finalists -> OOS 20%.
- OOS is never used to select or tune candidates.
- Long only, no leverage, fixed 10 USDT research notional.
- Output decision can only be REJECT or RESEARCH_PASS_NOT_LIVE.
"""
from __future__ import annotations
import argparse, hashlib, io, itertools, json, math, time, urllib.error, urllib.request, zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
import numpy as np
import pandas as pd

AUTHORIZATION='RESEARCH_ONLY'
BASE='https://data.binance.vision/data/spot/monthly/klines'
UA='tst-liquidity-sweep-factory-v2/1.0'
COLS=['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_buy_base','taker_buy_quote','ignore']

@dataclass(frozen=True)
class Cost: fee:float=.001; slip:float=.0005
@dataclass(frozen=True)
class Candidate:
    liq_lookback:int; sweep_depth_atr:float; choch_lookback:int; participation:str; btc_regime:bool
    @property
    def id(self):
        return f'L{self.liq_lookback}__D{self.sweep_depth_atr:.2f}__C{self.choch_lookback}__{self.participation}__BTC{int(self.btc_regime)}'

LOOKBACKS=[12,24,48]
DEPTHS=[0.0,0.15]
CHOCH=[3,6]
PARTICIPATION=['none','relvol125','taker55']
BTC_REGIME=[False,True]
CANDIDATES=[Candidate(*x) for x in itertools.product(LOOKBACKS,DEPTHS,CHOCH,PARTICIPATION,BTC_REGIME)]


def http_bytes(url):
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'*/*'})
    with urllib.request.urlopen(req,timeout=90) as r:return r.read()

def verified_zip(url):
    raw=http_bytes(url); exp=http_bytes(url+'.CHECKSUM').decode(errors='replace').strip().split()[0].lower(); got=hashlib.sha256(raw).hexdigest()
    if len(exp)!=64 or got!=exp: raise RuntimeError(f'checksum mismatch {url}')
    return raw

def parse_epoch(s):
    n=pd.to_numeric(s,errors='coerce'); med=float(n.dropna().median()) if n.notna().any() else 0
    unit='ns' if med>1e17 else ('us' if med>1e14 else 'ms')
    return pd.to_datetime(n,unit=unit,utc=True,errors='coerce')

def parse_zip(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        fs=[x for x in z.namelist() if x.lower().endswith('.csv')]
        if len(fs)!=1: raise RuntimeError(f'expected one csv, got {fs}')
        with z.open(fs[0]) as f: d=pd.read_csv(f,header=None)
    if len(d) and not str(d.iloc[0,0]).replace('-','').isdigit():d=d.iloc[1:].reset_index(drop=True)
    d=d.iloc[:,:len(COLS)].copy(); d.columns=COLS
    for c in ['open','high','low','close','volume','quote_volume','taker_buy_quote']:d[c]=pd.to_numeric(d[c],errors='coerce')
    d['ts']=parse_epoch(d.open_time)
    return d.dropna(subset=['ts','open','high','low','close'])

def months(start,end):
    a=pd.Timestamp(start).to_period('M'); b=(pd.Timestamp(end)-pd.Timedelta(microseconds=1)).to_period('M')
    return [str(x) for x in pd.period_range(a,b,freq='M')]

def load_symbol(symbol,start,end,min_coverage=.95):
    parts=[]; src=[]
    for m in months(start,end):
        url=f'{BASE}/{symbol}/15m/{symbol}-15m-{m}.zip'
        try:
            d=parse_zip(verified_zip(url));parts.append(d);src.append({'month':m,'rows':len(d),'ok':True});time.sleep(.01)
        except urllib.error.HTTPError as e:
            if e.code==404:src.append({'month':m,'rows':0,'ok':False});continue
            raise
    if not parts:raise RuntimeError(f'no data {symbol}')
    d=pd.concat(parts,ignore_index=True).sort_values('ts').drop_duplicates('ts').reset_index(drop=True)
    a=pd.Timestamp(start,tz='UTC');b=pd.Timestamp(end,tz='UTC');d=d[(d.ts>=a)&(d.ts<b)].copy().reset_index(drop=True)
    exp=max(1,int((b-a)/pd.Timedelta(minutes=15)));cov=len(d)/exp
    if cov<min_coverage:raise RuntimeError(f'{symbol} coverage {cov:.3%}')
    return d,src,cov

def atr14(d):
    prev=d.close.shift(1); tr=pd.concat([d.high-d.low,(d.high-prev).abs(),(d.low-prev).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean()

def enrich(d):
    x=d.copy();x['atr']=atr14(x);x['relvol']=x.quote_volume/x.quote_volume.rolling(20,min_periods=20).mean().replace(0,np.nan);x['taker_share']=x.taker_buy_quote/x.quote_volume.replace(0,np.nan)
    idx=x.set_index('ts')
    def tf(rule):
        r=idx[['open','high','low','close']].resample(rule,label='left',closed='left').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
        r['ema20']=r.close.ewm(span=20,adjust=False,min_periods=20).mean();r['ema50']=r.close.ewm(span=50,adjust=False,min_periods=50).mean();r['bull']=(r.close>r.ema20)&(r.ema20>r.ema50)
        # shift one completed HTF bar to avoid leaking current incomplete bar.
        return r[['bull']].shift(1)
    h1=tf('1h').rename(columns={'bull':'bull1h'});h4=tf('4h').rename(columns={'bull':'bull4h'})
    x=x.join(h1.reindex(x.ts,method='ffill').set_index(x.index)).join(h4.reindex(x.ts,method='ffill').set_index(x.index))
    return x

def attach_btc_regime(frames):
    btc=frames['BTCUSDT'][['ts','bull4h']].rename(columns={'bull4h':'btc_bull4h'}).set_index('ts')
    out={}
    for s,d in frames.items():
        z=d.copy();z['btc_bull4h']=btc.reindex(z.ts,method='ffill').to_numpy().reshape(-1);out[s]=z
    return out

def metrics(pnls,rets,initial=100.0):
    p=np.asarray(pnls,float);r=np.asarray(rets,float)
    if len(p)==0:return {'trades':0,'winRate':0.0,'pf':0.0,'avgPct':0.0,'net':0.0,'maxDDPct':0.0}
    gp=p[p>0].sum();gl=abs(p[p<0].sum());pf=(gp/gl if gl>0 else (999.0 if gp>0 else 0.0));eq=initial+np.cumsum(p);peak=np.maximum.accumulate(np.r_[initial,eq])[1:];dd=np.max((peak-eq)/peak*100) if len(eq) else 0
    return {'trades':int(len(p)),'winRate':float((p>0).mean()*100),'pf':float(pf),'avgPct':float(r.mean()),'net':float(p.sum()),'maxDDPct':float(dd)}

def simulate(d,cost,cand,start_i,end_i,symbol):
    o=d.open.to_numpy(float);h=d.high.to_numpy(float);l=d.low.to_numpy(float);cl=d.close.to_numpy(float);atr=d.atr.to_numpy(float);rv=d.relvol.to_numpy(float);tk=d.taker_share.to_numpy(float)
    b1=d.bull1h.fillna(False).to_numpy(bool);b4=d.bull4h.fillna(False).to_numpy(bool);bb=d.btc_bull4h.fillna(False).to_numpy(bool)
    n=len(d);liq=pd.Series(l).rolling(cand.liq_lookback,min_periods=cand.liq_lookback).min().shift(1).to_numpy(float)
    rh=pd.Series(h).rolling(cand.choch_lookback,min_periods=cand.choch_lookback).max().shift(1).to_numpy(float)
    pnls=[];rets=[];by_symbol=[];pos=None;armed=None;pending=False
    warm=max(80,cand.liq_lookback+5,cand.choch_lookback+5);a=max(start_i,warm);b=min(end_i,n)
    for i in range(a,b):
        if pending and pos is None:
            ep=o[i]*(1+cost.slip);qty=10.0/ep;pos={'e':ep,'q':qty,'stop':armed['stop'],'i':i};pending=False;armed=None
        if pos is not None:
            stop=pos['stop'];target=pos['e']+2.5*(pos['e']-stop)
            reason=None;xp=None
            if l[i]<=stop:xp=stop*(1-cost.slip);reason='stop'
            elif h[i]>=target:xp=target;reason='target'
            elif i-pos['i']>=96:xp=o[i]*(1-cost.slip);reason='timeout'
            elif not b1[i]:xp=o[i]*(1-cost.slip);reason='trend_loss'
            if xp is not None:
                q=pos['q'];gross=q*(xp-pos['e']);fees=cost.fee*q*(pos['e']+xp);pnl=gross-fees;pnls.append(pnl);rets.append(pnl/(q*pos['e'])*100);by_symbol.append(symbol);pos=None
            continue
        if armed is not None:
            if i>armed['expires']:armed=None
            else:
                part=(cand.participation=='none' or (cand.participation=='relvol125' and rv[i]>=1.25) or (cand.participation=='taker55' and tk[i]>=.55))
                regime=b1[i] and b4[i] and ((not cand.btc_regime) or bb[i])
                if regime and part and np.isfinite(rh[i]) and cl[i]>rh[i]:pending=True
                continue
        if not (np.isfinite(liq[i]) and np.isfinite(atr[i]) and atr[i]>0):continue
        regime=b1[i] and b4[i] and ((not cand.btc_regime) or bb[i])
        sweep=l[i] < (liq[i]-cand.sweep_depth_atr*atr[i]) and cl[i]>liq[i]
        if regime and sweep:
            stop=l[i]-.20*atr[i];
            if stop<cl[i]:armed={'stop':stop,'expires':min(i+8,b-1)}
    return pnls,rets,by_symbol

def evaluate(frames,cand,segment,cost):
    allp=[];allr=[];symstats={}
    for s,d in frames.items():
        n=len(d);cuts=[0,int(.6*n),int(.8*n),n];mp={'DISC':(cuts[0],cuts[1]),'VAL':(cuts[1],cuts[2]),'OOS':(cuts[2],cuts[3])};a,b=mp[segment]
        p,r,_=simulate(d,cost,cand,a,b,s);allp+=p;allr+=r;symstats[s]=metrics(p,r)
    m=metrics(allp,allr);m['profitableSymbols']=sum(1 for v in symstats.values() if v['net']>0);m['symbols']=symstats;return m

def score(m):return m['pf']*math.log1p(m['trades']) + max(0,m['avgPct'])*8 - m['maxDDPct']*.15

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--symbols',nargs='+',default=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','LINKUSDT']);ap.add_argument('--start',default='2024-09-01');ap.add_argument('--end',default='2026-09-01');ap.add_argument('--output',default='artifacts/liquidity-sweep-v2/report.json');a=ap.parse_args()
    raw={};meta={}
    for s in a.symbols:
        print('Loading',s,flush=True);d,src,cov=load_symbol(s,a.start,a.end);raw[s]=enrich(d);meta[s]={'coverage':cov,'rows':len(d),'sources':src}
    frames=attach_btc_regime(raw);cost=Cost();stress=Cost(slip=.0010)
    disc=[]
    for j,c in enumerate(CANDIDATES,1):
        m=evaluate(frames,c,'DISC',cost);eligible=m['trades']>=100 and m['pf']>=1.15 and m['avgPct']>0 and m['maxDDPct']<=8 and m['profitableSymbols']>=3
        disc.append({'id':c.id,'candidate':asdict(c),'metrics':m,'eligible':eligible,'score':score(m)})
        if j%18==0:print(f'Discovery {j}/{len(CANDIDATES)}',flush=True)
    dsel=sorted([x for x in disc if x['eligible']],key=lambda x:x['score'],reverse=True)[:20]
    val=[]
    for x in dsel:
        c=Candidate(**x['candidate']);m=evaluate(frames,c,'VAL',cost);passed=m['trades']>=30 and m['pf']>=1.10 and m['avgPct']>0 and m['maxDDPct']<=5 and m['profitableSymbols']>=3;val.append({'id':c.id,'candidate':asdict(c),'metrics':m,'passed':passed,'score':score(m)})
    finalists=sorted([x for x in val if x['passed']],key=lambda x:x['score'],reverse=True)[:8]
    oos=[]
    for x in finalists:
        c=Candidate(**x['candidate']);m=evaluate(frames,c,'OOS',cost);ms=evaluate(frames,c,'OOS',stress);passed=m['trades']>=30 and m['pf']>=1.15 and m['avgPct']>0 and m['maxDDPct']<=5 and m['profitableSymbols']>=3 and ms['pf']>=1.0 and ms['avgPct']>=0;oos.append({'id':c.id,'candidate':asdict(c),'metrics':m,'stress':ms,'passed':passed})
    passing=[x['id'] for x in oos if x['passed']]
    report={'engine':'LIQUIDITY_SWEEP_FACTORY_V2','authorization':AUTHORIZATION,'liveTrading':False,'source':'Binance Vision Spot monthly 15m','checksumVerification':True,'window':{'start':a.start,'end':a.end,'split':'60/20/20','timeframe':'15m'},'selectionProtocol':{'oosUsedForSelection':False,'nextBarExecution':True,'stopWinsIfStopAndTargetTouchSameBar':True},'candidateCount':len(CANDIDATES),'data':meta,'discovery':{'eligible':len(dsel),'top':dsel},'validation':{'passed':len(finalists),'frozenFinalists':[x['id'] for x in finalists],'results':val},'oos':{'passed':len(passing),'results':oos},'promotionGate':{'decision':'RESEARCH_PASS_NOT_LIVE' if passing else 'REJECT','passingCandidates':passing,'canEnableLiveTrading':False,'nextStage':'SHADOW_ONLY' if passing else 'NONE'}}
    out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,indent=2,allow_nan=False));print(json.dumps({'candidateCount':len(CANDIDATES),'discoveryEligible':len(dsel),'validationPassed':len(finalists),'oosPassed':len(passing),'promotionGate':report['promotionGate']},indent=2))
if __name__=='__main__':main()
