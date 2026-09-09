#!/usr/bin/env python3
"""Research-only intraday edge validation v2.

Extends the existing v1 scanner without changing live trading. It compares
breakout-immediate vs breakout-retest and several independent Spot setup
families under the same next-bar execution, pessimistic intrabar ordering,
chronological OOS, symbol holdout, cost stress, regime split, and clustered
bootstrap. Production promotion is always blocked because the seed universe is
still based on current Binance listings and therefore does not yet eliminate
historical delisting survivorship.
"""
from __future__ import annotations

import hashlib, json, math, pathlib, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
from research import intraday_barrier_edge_scanner_v1 as base

base.DAYS = 180
base.MAX_SYMBOLS = 70
base.CURRENT_MIN_QV = 6_000_000
base.EVENT_MIN_QV24 = 3_000_000
base.EXCLUDED = set(base.EXCLUDED) | {'U'}

OUT = pathlib.Path('validation/edges/intraday-edge-validation-v2-latest.json')
COSTS = {'normal':0.0028, 'stress_1_5x':0.0042, 'severe':0.0060}
CONTRACTS = {
    'R15': {'tp':0.018, 'sl':0.010, 'horizon_bars':20},
    'R18': {'tp':0.0252, 'sl':0.014, 'horizon_bars':32},
}
MIN_DISCOVERY = 100
MIN_VALIDATION = 40
FDR_Q = 0.10
BOOTSTRAP_N = 1000
RNG_SEED = 20260909


def log(x): print(f"[{time.strftime('%H:%M:%S')}] {x}", flush=True)


def augment(d, btc):
    x = base.add_symbol_features(d, btc).sort_values('ts').copy()
    c=x.close; r15=c.pct_change(); prev=c.shift(1)
    x['ret_15m']=r15; x['ret_24h']=c.pct_change(96)
    x['ema50']=c.ewm(span=50,adjust=False).mean()
    tr=pd.concat([(x.high-x.low),(x.high-prev).abs(),(x.low-prev).abs()],axis=1).max(axis=1)
    x['atr14']=tr.rolling(14,min_periods=10).mean(); x['atr96']=tr.rolling(96,min_periods=48).mean()
    x['compression']=x.atr14/x.atr96.replace(0,np.nan)
    x['pullback_8h']=c/x.high.rolling(32,min_periods=16).max()-1
    body=(x.close-x.open).abs()
    x['upper_wick_ratio']=(x.high-x[['open','close']].max(axis=1))/np.maximum(body,c*0.0001)
    x['lower_wick_ratio']=(x[['open','close']].min(axis=1)-x.low)/np.maximum(body,c*0.0001)
    x['btc_ret_15m']=x.btc_close.pct_change(); x['btc_ret_24h']=x.btc_close.pct_change(96)
    cov=x.ret_15m.rolling(96,min_periods=48).cov(x.btc_ret_15m)
    var=x.btc_ret_15m.rolling(96,min_periods=48).var().replace(0,np.nan)
    x['beta_btc']=(cov/var).clip(-2,4)
    x['btc_lag_gap_1h']=x.beta_btc*x.btc_ret_1h-x.ret_1h
    return x


def cross_sectional(d):
    x=base.add_cross_sectional(d)
    for c in ['ret_24h','btc_lag_gap_1h']:
        x[c+'_rank']=x.groupby('ts')[c].rank(pct=True,method='average')
    x['breadth_1h']=x.groupby('ts').ret_1h.transform(lambda s:float((s>0).mean()))
    x['breadth_4h']=x.groupby('ts').ret_4h.transform(lambda s:float((s>0).mean()))
    return x


def attach_regime(d, btc):
    b=btc.sort_values('ts').copy(); c=b.close
    b['b1']=c.pct_change(4); b['b4']=c.pct_change(16); b['b24']=c.pct_change(96)
    b['e20']=c.ewm(span=20,adjust=False).mean(); b['e50']=c.ewm(span=50,adjust=False).mean()
    b['rv']=c.pct_change().rolling(96,min_periods=48).std(ddof=0)
    b['rv_med']=b.rv.rolling(96*30,min_periods=96*5).median()
    x=d.merge(b[['ts','close','b1','b4','b24','e20','e50','rv','rv_med']].rename(columns={'close':'btc_px'}),on='ts',how='left')
    def f(r):
        b1=float(r.b1 or 0); b4=float(r.b4 or 0); b24=float(r.b24 or 0)
        px=float(r.btc_px or 0); e20=float(r.e20 or 0); e50=float(r.e50 or 0)
        rv=float(r.rv or 0); med=float(r.rv_med) if pd.notna(r.rv_med) and r.rv_med>0 else max(rv,1e-9)
        br1=float(r.breadth_1h); br4=float(r.breadth_4h)
        if b4<=-0.035 or (b24<=-0.06 and rv>=1.5*med): return 'PANIC_HIGH_VOL_BEAR'
        if b24<=-0.025 and b1>0.006 and br1>=0.55: return 'POST_CRASH_RECOVERY'
        if px>e20>e50 and b4>0.010 and br4>=0.60: return 'STRONG_BULL'
        if px>=e20*.995 and e20>=e50*.992 and b4>-0.006 and br4>=0.48: return 'WEAK_BULL'
        if px<e20<e50 and (b4<-0.008 or br4<0.40): return 'WEAK_BEAR'
        return 'SIDEWAYS_COMPRESSION'
    x['regime']=x.apply(f,axis=1)
    return x


def setup_masks(d):
    immediate=(
        d.breakout_24h.between(0,.010) & (d.ret_1h_rank>=.70) &
        (d.volume_ratio>=1.20) & (d.taker_buy_ratio>=.56) &
        (d.upper_wick_ratio<=1.25) & (d.rsi14<=76)
    )
    prev_break=immediate.groupby(d.symbol).shift(1,fill_value=False)
    prev_res=d.groupby('symbol').prior24_high.shift(1)
    prev_vol=d.groupby('symbol').quote_volume.shift(1)
    retest=(
        prev_break & (d.low<=prev_res*1.0035) & (d.low>=prev_res*.985) &
        (d.close>=prev_res*.999) & (d.quote_volume<=prev_vol*.90) &
        (d.taker_buy_ratio>=.55) & (d.close>=d.ema9*.997)
    )
    rs_pullback=(
        (d.ret_4h_rank>=.80) & (d.rs_4h_rank>=.80) & d.pullback_8h.between(-.035,-.006) &
        (d.close>=d.ema21*.995) & (d.ema21>=d.ema50*.995) &
        (d.volume_ratio>=.90) & (d.taker_buy_ratio>=.55) & d.rsi14.between(45,72)
    )
    compression=(
        (d.compression<=.75) & d.breakout_24h.between(0,.012) &
        (d.volume_ratio>=1.35) & (d.taker_buy_ratio>=.57) &
        (d.ret_1h_rank>=.65) & (d.upper_wick_ratio<=1.0)
    )
    vol_exp=(
        (d.vol_4h_rank>=.80) & (d.ret_1h_rank>=.85) & (d.rs_1h_rank>=.70) &
        (d.volume_ratio_rank>=.75) & (d.taker_buy_ratio>=.56) &
        (d.close>=d.ema9) & (d.rsi14<=76)
    )
    crash=(
        (d.ret_4h_rank<=.10) & (d.rs_4h_rank<=.15) & (d.rsi14<=34) &
        (d.lower_wick_ratio>=1.0) & (d.accel_1h>0) &
        (d.taker_buy_ratio>=.54) & (d.volume_ratio>=1.10)
    )
    lag=(
        (d.btc_ret_1h>=.008) & (d.btc_lag_gap_1h_rank>=.90) &
        (d.rs_4h_rank>=.50) & (d.ret_15m>0) &
        (d.taker_buy_ratio>=.55) & (d.volume_ratio>=1.05)
    )
    return {
        'BREAKOUT_IMMEDIATE':immediate,'BREAKOUT_RETEST':retest,'RS_TREND_PULLBACK':rs_pullback,
        'COMPRESSION_BREAKOUT':compression,'VOLATILITY_EXPANSION':vol_exp,
        'CRASH_EXHAUSTION':crash,'BTC_ALT_LEAD_LAG':lag,
    }


def holdout(symbol): return int(hashlib.sha256(symbol.encode()).hexdigest()[:8],16)%5==0


def barrier(g,idx,c,event_ts,regime):
    if idx+1>=len(g): return None
    entry=float(g.open.iloc[idx+1]); tp=entry*(1+c['tp']); sl=entry*(1-c['sl'])
    end=min(len(g)-1,idx+c['horizon_bars']); outcome='TIMEOUT'; gross=None; mfe=mae=0.0
    for j in range(idx+1,end+1):
        hi=float(g.high.iloc[j]); lo=float(g.low.iloc[j]); mfe=max(mfe,hi/entry-1); mae=max(mae,1-lo/entry)
        hs=lo<=sl; ht=hi>=tp
        if hs and ht: outcome='SL_AMBIGUOUS'; gross=-c['sl']; break
        if hs: outcome='SL'; gross=-c['sl']; break
        if ht: outcome='TP'; gross=c['tp']; break
    if gross is None: gross=max(-c['sl'],min(c['tp'],float(g.close.iloc[end])/entry-1))
    return {'gross':gross,'outcome':outcome,'mfe':mfe,'mae':mae,'day':pd.Timestamp(event_ts).strftime('%Y-%m-%d'),'regime':regime}


def summary(rows,cost):
    if not rows:return {'n':0}
    net=np.asarray([r['gross']-cost for r in rows]); pos=np.maximum(net,0).sum(); neg=np.maximum(-net,0).sum()
    return {'n':len(rows),'meanNet':float(net.mean()),'medianNet':float(np.median(net)),
            'profitFactor':float(pos/neg) if neg>0 else 999.0,'tpRate':sum(r['outcome']=='TP' for r in rows)/len(rows),
            'medianMFE':float(np.median([r['mfe'] for r in rows])),'medianMAE':float(np.median([r['mae'] for r in rows]))}


def pmean(rows,cost):
    if len(rows)<2:return 1.0
    a=np.asarray([r['gross']-cost for r in rows]); sd=float(a.std(ddof=1))
    if sd<=0:return 0.0 if a.mean()>0 else 1.0
    return 0.5*math.erfc(float(a.mean())/(sd/math.sqrt(len(a)))/math.sqrt(2))


def bootstrap(rows,cost):
    by=defaultdict(list)
    for r in rows:by[r['day']].append(r['gross']-cost)
    days=sorted(by)
    if len(rows)<MIN_VALIDATION or len(days)<10:return {'status':'INSUFFICIENT','n':len(rows),'days':len(days)}
    rng=np.random.default_rng(RNG_SEED); means=[]
    for _ in range(BOOTSTRAP_N):
        vals=[]
        for day in rng.choice(days,size=len(days),replace=True):vals.extend(by[str(day)])
        means.append(float(np.mean(vals)))
    return {'status':'OK','days':len(days),'p05':float(np.quantile(means,.05)),'p50':float(np.quantile(means,.5)),'p95':float(np.quantile(means,.95))}


def regime_stats(rows,cost):
    by=defaultdict(list)
    for r in rows:by[r['regime']].append(r)
    return {k:summary(v,cost) for k,v in sorted(by.items())}


def bh(hs):
    order=sorted(range(len(hs)),key=lambda i:hs[i]['discoveryP']); m=len(order); adj=[1.0]*len(hs); run=1.0
    for rank in range(m,0,-1):
        i=order[rank-1]; run=min(run,hs[i]['discoveryP']*m/rank); adj[i]=min(1.0,run)
    for i,h in enumerate(hs):h['qValue']=adj[i]


def valid(s,n=MIN_VALIDATION): return s.get('n',0)>=n and s.get('meanNet',-1)>0 and s.get('profitFactor',0)>=1.05


def main():
    started=time.time(); syms=base.universe(); all_syms=syms if 'BTCUSDT' in syms else ['BTCUSDT']+syms
    data={}; fail={}; log(f'universe={len(syms)}')
    with ThreadPoolExecutor(max_workers=base.DOWNLOAD_WORKERS) as ex:
        fut={ex.submit(base.klines,s):s for s in all_syms}
        for n,f in enumerate(as_completed(fut),1):
            s=fut[f]
            try:data[s]=f.result(); log(f'data {n}/{len(all_syms)} {s} bars={len(data[s])}')
            except Exception as e:fail[s]=str(e); log(f'data {n}/{len(all_syms)} {s} FAIL {e}')
    if 'BTCUSDT' not in data:raise RuntimeError('BTC unavailable')
    loaded=[s for s in syms if s in data and s!='BTCUSDT']; frames=[augment(data[s],data['BTCUSDT']) for s in loaded]
    d=attach_regime(cross_sectional(pd.concat(frames,ignore_index=True)),data['BTCUSDT'])
    d=d[(d.qv24>=base.EVENT_MIN_QV24)].replace([np.inf,-np.inf],np.nan)
    d=d.dropna(subset=['ret_1h','ret_4h','rs_1h','rs_4h','volume_ratio','taker_buy_ratio','rsi14','regime'])
    times=sorted(d.ts.unique()); t60=times[int(len(times)*.60)]; t80=times[int(len(times)*.80)]
    d['split']=np.where(d.ts<t60,'DISCOVERY',np.where(d.ts<t80,'CALIBRATION','TEST')); d['holdout']=d.symbol.map(holdout)
    masks=setup_masks(d)
    # Raw timelines are mandatory for next-bar labels; filtered rows must never change entry timing.
    grouped={s:data[s].sort_values('ts').reset_index(drop=True) for s in loaded}; lookup={s:{pd.Timestamp(t):i for i,t in enumerate(g.ts)} for s,g in grouped.items()}
    hs=[]
    for name,mask in masks.items():
        selected=d[mask.fillna(False)]
        for cname,c in CONTRACTS.items():
            b={'DISCOVERY':[],'CALIBRATION':[],'TEST':[],'SYMBOL_HOLDOUT':[]}
            for r in selected.itertuples(index=False):
                bucket='SYMBOL_HOLDOUT' if r.holdout else r.split; idx=lookup[r.symbol].get(pd.Timestamp(r.ts))
                if idx is not None:
                    z=barrier(grouped[r.symbol],idx,c,r.ts,r.regime)
                    if z:b[bucket].append(z)
            normal={k:summary(v,COSTS['normal']) for k,v in b.items()}; stress={k:summary(v,COSTS['stress_1_5x']) for k,v in b.items()}
            hs.append({'setup':name,'contract':cname,'normal':normal,'stress':stress,'discoveryP':pmean(b['DISCOVERY'],COSTS['normal']),
                       'testBootstrap':bootstrap(b['TEST'],COSTS['normal']),'testRegimes':regime_stats(b['TEST'],COSTS['normal'])})
    bh(hs)
    for h in hs:
        n=h['normal']; st=h['stress']; boot=h['testBootstrap']; regs=[v for v in h['testRegimes'].values() if v.get('n',0)>=30]
        h['discoveryPass']=n['DISCOVERY'].get('n',0)>=MIN_DISCOVERY and n['DISCOVERY'].get('meanNet',-1)>0 and n['DISCOVERY'].get('profitFactor',0)>=1.15 and h['qValue']<=FDR_Q
        h['calibrationPass']=valid(n['CALIBRATION']); h['testPass']=valid(n['TEST']); h['symbolHoldoutPass']=valid(n['SYMBOL_HOLDOUT'])
        h['stressPass']=valid(st['TEST']) and valid(st['SYMBOL_HOLDOUT']); h['bootstrapPass']=boot.get('status')=='OK' and boot.get('p05',-1)>0
        h['regimeStable']=len(regs)>=2 and sum(v.get('meanNet',-1)>0 for v in regs)>=max(2,math.ceil(len(regs)*.6))
        h['statisticalEvidencePass']=all([h['discoveryPass'],h['calibrationPass'],h['testPass'],h['symbolHoldoutPass'],h['stressPass'],h['bootstrapPass'],h['regimeStable']])
        h['productionEligible']=False
    survivors=[h for h in hs if h['statisticalEvidencePass']]
    compare=[]
    for cname in CONTRACTS:
        a=next(h for h in hs if h['setup']=='BREAKOUT_IMMEDIATE' and h['contract']==cname); r=next(h for h in hs if h['setup']=='BREAKOUT_RETEST' and h['contract']==cname)
        compare.append({'contract':cname,'immediateTest':a['normal']['TEST'],'retestTest':r['normal']['TEST'],
                        'deltaMeanNet':r['normal']['TEST'].get('meanNet',0)-a['normal']['TEST'].get('meanNet',0)})
    payload={'engine':'INTRADAY_EDGE_VALIDATION_V2','authorization':'RESEARCH_ONLY','liveTrading':False,'automaticPromotion':False,
             'productionPromotionBlocked':True,'productionBlockers':['CURRENT_LISTING_SURVIVORSHIP_NOT_YET_ELIMINATED','WALK_FORWARD_NOT_IN_THIS_RUN'],
             'days':base.DAYS,'interval':base.INTERVAL,'symbolsRequested':syms,'symbolsLoaded':loaded,'failures':fail,
             'validationDesign':{'entryTiming':'NEXT_15M_OPEN','sameBarTpSlResolution':'SL_FIRST_PESSIMISTIC','timeSplit':'60/20/20','symbolHoldout':'sha256(symbol)%5==0','costs':COSTS,'bootstrap':'day-cluster','BH_FDR':FDR_Q},
             'hypothesisCount':len(hs),'survivorCount':len(survivors),'survivors':survivors,'breakoutRetestVsImmediate':compare,'allHypotheses':hs,
             'runtimeSeconds':round(time.time()-started,2),'note':'No result can promote live until delisted-history survivorship and walk-forward gates are solved.'}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(payload,indent=2,sort_keys=True,allow_nan=False)); log(f"DONE hypotheses={len(hs)} survivors={len(survivors)}")

if __name__=='__main__':main()
