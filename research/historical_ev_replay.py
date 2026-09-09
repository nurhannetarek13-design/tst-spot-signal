#!/usr/bin/env python3
from __future__ import annotations

"""Historical point-in-time EV replay from Binance Vision Spot archives.

Research-only. Generates a frozen historical model/evidence package compatible
with freqtrade/shadow_ev_model.py. It never places orders and never changes live
state. Historical evidence can only be used after chronological OOS,
walk-forward, unseen-symbol and calibration gates pass.
"""

import csv, datetime as dt, hashlib, io, json, math, os, pathlib, statistics, sys, time, urllib.error, urllib.request, zipfile

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'freqtrade'))
import shadow_ev_model as m

BASE='https://data.binance.vision/data/spot/monthly/klines'
OUT=ROOT/'freqtrade'/'historical_ev_evidence.json'
UA='tst-historical-ev-replay/1.0'
MONTHS=max(6,min(12,int(os.getenv('HIST_EV_MONTHS','8'))))
COST_PCT=float(os.getenv('HIST_EV_ROUND_TRIP_COST_PCT','0.28'))
EXTRA_STRESS_PCT=float(os.getenv('HIST_EV_EXTRA_STRESS_PCT','0.28'))
MIN_HIST=max(400,int(os.getenv('HIST_EV_MIN_SAMPLE','600')))
MIN_TEST=max(60,int(os.getenv('HIST_EV_MIN_TEST','100')))
TOP_N=max(1,int(os.getenv('HIST_EV_TOP_N','3')))
MIN_GAP_BARS=max(1,int(os.getenv('HIST_EV_MIN_GAP_BARS','4')))
SYMBOLS=['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','TRXUSDT','LINKUSDT','AVAXUSDT','SUIUSDT','LTCUSDT','BCHUSDT','XLMUSDT','DOTUSDT','NEARUSDT','AAVEUSDT','UNIUSDT','ETCUSDT','FILUSDT','ATOMUSDT','ARBUSDT','OPUSDT','HBARUSDT']
RULES={'bar_interval':'15m','candidate_score_min':86,'top_n_cross_sectional':TOP_N,'min_taker_buy_share':0.56,'min_relative_volume':1.50,'require_1h_up':True,'require_4h_up':True,'require_breakout_or_reclaim':True,'max_positive_24h_extension':2.00,'decluster_bars':MIN_GAP_BARS,'cost_pct':COST_PCT,'stress_extra_pct':EXTRA_STRESS_PCT}
RULESET_HASH=hashlib.sha256(json.dumps(RULES,sort_keys=True).encode()).hexdigest()[:16]

def months():
    d=dt.datetime.now(dt.timezone.utc).date().replace(day=1); y,mo=d.year,d.month; out=[]
    for _ in range(MONTHS):
        mo-=1
        if mo==0: mo=12; y-=1
        out.append(f'{y:04d}-{mo:02d}')
    return list(reversed(out))

def download(symbol,month,retries=3):
    url=f'{BASE}/{symbol}/15m/{symbol}-15m-{month}.zip'; last=None
    for i in range(retries):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':UA})
            with urllib.request.urlopen(req,timeout=35) as r: raw=r.read()
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names=[n for n in zf.namelist() if n.lower().endswith('.csv')]
                if not names:return []
                return list(csv.reader(io.StringIO(zf.read(names[0]).decode('utf-8-sig',errors='replace'))))
        except urllib.error.HTTPError as exc:
            if exc.code==404:return []
            last=exc
        except Exception as exc:last=exc
        time.sleep(.5+i)
    raise RuntimeError(f'download failed {symbol} {month}: {last}')

def ts_ms(raw):
    x=int(float(raw))
    if x>10**14:x//=1000
    return x

def parse(rows):
    out=[]
    for r in rows or []:
        if len(r)<11:continue
        try:out.append({'ts':ts_ms(str(r[0]).strip())//1000,'open':float(r[1]),'high':float(r[2]),'low':float(r[3]),'close':float(r[4]),'volume':float(r[5]),'quote_volume':float(r[7]),'trades':int(float(r[8])),'taker_buy_quote':float(r[10])})
        except Exception:pass
    out.sort(key=lambda x:x['ts']); return out

def pct_rank(values):
    if not values:return {}
    items=sorted(values.items(),key=lambda kv:(kv[1],kv[0])); n=len(items)
    if n==1:return {items[0][0]:1.0}
    return {sym:i/(n-1) for i,(sym,_) in enumerate(items)}

def med(xs):return statistics.median(xs) if xs else 0.0

def atr_pct(bars,i,n=14):
    if i<n:return 0.0
    trs=[]
    for j in range(i-n+1,i+1):
        prev=bars[j-1]['close']; trs.append(max(bars[j]['high']-bars[j]['low'],abs(bars[j]['high']-prev),abs(bars[j]['low']-prev)))
    c=bars[i]['close']; return (sum(trs)/len(trs))/c if c>0 else 0.0

def raw_feature(bars,i,btc,bi):
    if i<100 or bi<20:return None
    c=bars[i]['close']; qv=bars[i]['quote_volume']
    if c<=0 or qv<=0:return None
    r15=c/bars[i-1]['close']-1; r1h=c/bars[i-4]['close']-1; r4h=c/bars[i-16]['close']-1; r24=c/bars[i-96]['close']-1
    prev1h=bars[i-4]['close']/bars[i-8]['close']-1; accel=r1h-prev1h
    btc4h=btc[bi]['close']/btc[bi-16]['close']-1; btc1h=btc[bi]['close']/btc[bi-4]['close']-1
    medq=med([x['quote_volume'] for x in bars[i-20:i]]); volx=qv/medq if medq>0 else 0.0; taker=bars[i]['taker_buy_quote']/qv
    liq=math.log1p(max(0.0,med([x['quote_volume'] for x in bars[i-16:i+1]])))
    rh=max(x['high'] for x in bars[i-8:i]); rl=min(x['low'] for x in bars[i-8:i]); breakout=c>=rh*.9995; pb=(rh-rl)/rh if rh>0 else 0.0; reclaim=.003<=pb<=.06 and c>=rh*.995 and c>bars[i-1]['close']
    score=0; score+=10 if r15>0 else 0; score+=10 if r1h>0 else 0; score+=10 if r4h>0 else 0; score+=15 if taker>=.56 else 0; score+=5 if taker>=.62 else 0; score+=15 if volx>=1.5 else 0; score+=5 if volx>=2 else 0; score+=15 if reclaim else (10 if breakout else 0); score+=5 if r24>0 else 0; score+=5 if 0<r24<.20 else 0; score=min(100,score)
    ap=atr_pct(bars,i); risk=min(.018,max(.0062,1.30*ap)); reward=min(.035,max(.0090,1.80*risk))
    return {'r15':r15,'r1h':r1h,'r4h':r4h,'r24':r24,'btc1h':btc1h,'btc4h':btc4h,'rs_vs_btc_4h':r4h-btc4h,'accel1h':accel,'volume_expansion':volx,'liquidity':liq,'taker_buy_share':taker,'breakout':breakout,'reclaim':reclaim,'score':score,'risk_pct':risk,'reward_pct':reward}

def lane(r24):
    x=r24*100
    return 'EXTREME' if x>=120 else ('EXPLOSIVE' if x>=25 else ('MID' if x>=5 else 'NORMAL'))

def regime(btc1h,btc4h,b1,b4):
    if btc1h<=-.015 and b1<=.25:return 'PANIC_HIGH_VOL_BEAR'
    if btc4h>=.015 and b4>=.65:return 'STRONG_BULL'
    if btc4h>0 and b4>=.52:return 'WEAK_BULL'
    if btc4h<=-.010 and b4<=.35:return 'WEAK_BEAR'
    if btc1h>=.012 and b1>=.65 and btc4h<0:return 'POST_CRASH_RECOVERY'
    return 'SIDEWAYS_COMPRESSION'

def outcome(bars,i,row):
    if i+4>=len(bars):return None
    entry=bars[i]['close']; target=entry*(1+row['risk_reward']['reward']); stop=entry*(1-row['risk_reward']['risk']); future=bars[i+1:i+5]; out=None; hit=None
    for k,b in enumerate(future,1):
        tp=b['high']>=target; sl=b['low']<=stop
        if tp and sl:out,hit='SL',k;break
        if sl:out,hit='SL',k;break
        if tp:out,hit='TP',k;break
    mfe=(max(b['high'] for b in future)/entry-1)*100; mae=(min(b['low'] for b in future)/entry-1)*100
    if out=='TP':gross=(target/entry-1)*100; label=1
    elif out=='SL':gross=(stop/entry-1)*100; label=0
    else:gross=(future[-1]['close']/entry-1)*100; label=None
    return {**row,'entry':entry,'target':target,'stop':stop,'mfe_pct':mfe,'mae_pct':mae,'tp_before_sl':label,'path_outcome':out,'holding_min':hit*15 if hit else 60,'sim_gross_pct':gross,'sim_net_pct':gross-COST_PCT,'complete':True}

def fit_predict(train,test):
    if len(train)<120 or len(test)<25:return [],[]
    cut=max(90,int(len(train)*.8)); fit,cal=train[:cut],train[cut:]; schema=m._fit_schema(fit); xf=[m._vec(r,schema) for r in fit]; xt=[m._vec(r,schema) for r in test]; y=[int(r['tp_before_sl']) for r in fit]
    if len(set(y))<2 or min(sum(y),len(y)-sum(y))<25:return [],[]
    pw=m._fit_logistic(xf,y); platt=[0.0,1.0]
    if len(cal)>=30:
        yc=[int(r['tp_before_sl']) for r in cal]
        if len(set(yc))==2:
            xc=[m._vec(r,schema) for r in cal]; platt=m._fit_platt([m._sigmoid(m._dot(pw,x)) for x in xc],yc)
    probs=[m._calibrate(m._sigmoid(m._dot(pw,x)),platt) for x in xt]
    schema2=m._fit_schema(train); xa=[m._vec(r,schema2) for r in train]; xte=[m._vec(r,schema2) for r in test]; nw=m._fit_linear(xa,[float(r['sim_net_pct']) for r in train]); evs=[m._dot(nw,x) for x in xte]
    return probs,evs

def metric(test,probs,evs):
    if not test or not probs or not evs:return {'n':len(test),'pass':False}
    k=min(len(test),max(25,int(len(test)*.2))); ids=sorted(range(len(test)),key=lambda i:evs[i],reverse=True)[:k]; vals=[float(test[i]['sim_net_pct'])-EXTRA_STRESS_PCT for i in ids]; pp=[probs[i] for i in ids]; yy=[int(test[i]['tp_before_sl']) for i in ids]
    mean=sum(vals)/len(vals); md=statistics.median(vals); hit=sum(v>0 for v in vals)/len(vals); gap=abs(sum(pp)/len(pp)-sum(yy)/len(yy)); ci=m._bootstrap_mean_ci(vals); lo=ci[0]; passed=mean>0 and md>-.05 and hit>=.50 and gap<=.12 and lo is not None and lo>-.10
    return {'n':len(test),'top_n':len(vals),'mean_stressed_net_pct':round(mean,4),'median_stressed_net_pct':round(md,4),'hit_rate_stressed':round(hit,4),'probability_calibration_gap_top':round(gap,4),'bootstrap95':ci,'pass':passed}

def build_model(rows):
    cut=max(120,int(len(rows)*.8)); fit,cal=rows[:cut],rows[cut:]; schema_fit=m._fit_schema(fit); xf=[m._vec(r,schema_fit) for r in fit]; y=[int(r['tp_before_sl']) for r in fit]; pw=m._fit_logistic(xf,y); platt=[0.0,1.0]
    if len(cal)>=30 and len(set(int(r['tp_before_sl']) for r in cal))==2:
        xc=[m._vec(r,schema_fit) for r in cal]; yc=[int(r['tp_before_sl']) for r in cal]; platt=m._fit_platt([m._sigmoid(m._dot(pw,x)) for x in xc],yc)
    schema=m._fit_schema(rows); xa=[m._vec(r,schema) for r in rows]; pw=m._fit_logistic(xa,[int(r['tp_before_sl']) for r in rows]); reg={}
    for key,target in {'net_pct':[float(r['sim_net_pct']) for r in rows],'mfe_pct':[float(r['mfe_pct']) for r in rows],'mae_pct':[float(r['mae_pct']) for r in rows],'holding_min':[float(r['holding_min']) for r in rows]}.items():reg[key]=m._fit_linear(xa,target)
    return {'feature_version':m.FEATURE_VERSION,'trained_at':time.time(),'schema':schema,'probability_weights':pw,'platt':platt,'regression_weights':reg}

def main():
    mos=months(); data={}; failures=[]
    for sym in SYMBOLS:
        bars=[]
        for mo in mos:
            try:bars.extend(parse(download(sym,mo)))
            except Exception as exc:failures.append({'symbol':sym,'month':mo,'error':f'{type(exc).__name__}:{str(exc)[:120]}'})
        ded={int(b['ts']):b for b in bars}; data[sym]=[ded[k] for k in sorted(ded)]
    btc=data.get('BTCUSDT') or []; bix={int(b['ts']):i for i,b in enumerate(btc)}; idxs={s:{int(b['ts']):i for i,b in enumerate(bs)} for s,bs in data.items()}
    core=[set(idxs[s]) for s in SYMBOLS[:8] if idxs.get(s)]
    timestamps=sorted(set.intersection(*core)) if btc and core else []; events=[]; last={}
    for ts in timestamps:
        bi=bix.get(ts)
        if bi is None or bi<100:continue
        raw={}
        for sym,bars in data.items():
            i=idxs[sym].get(ts); fr=raw_feature(bars,i,btc,bi) if i is not None else None
            if fr is not None:raw[sym]=(i,fr)
        if len(raw)<10 or 'BTCUSDT' not in raw:continue
        b1=sum(1 for _,fr in raw.values() if fr['r1h']>0)/len(raw); b4=sum(1 for _,fr in raw.values() if fr['r4h']>0)/len(raw); ranks={k:pct_rank({s:fr[k] for s,(_,fr) in raw.items()}) for k in ('r1h','r4h','rs_vs_btc_4h','accel1h','volume_expansion','liquidity')}
        opp={s:.25*ranks['r1h'][s]+.20*ranks['r4h'][s]+.20*ranks['rs_vs_btc_4h'][s]+.15*ranks['accel1h'][s]+.10*ranks['volume_expansion'][s]+.10*ranks['liquidity'][s] for s in raw}; opp_pct=pct_rank(opp); order=sorted(opp,key=lambda s:(opp[s],s),reverse=True); opp_rank={s:i+1 for i,s in enumerate(order)}; bf=raw['BTCUSDT'][1]; rg=regime(bf['btc1h'],bf['btc4h'],b1,b4)
        for sym,(i,fr) in raw.items():
            if sym=='BTCUSDT' or fr['score']<RULES['candidate_score_min'] or fr['r1h']<=0 or fr['r4h']<=0 or fr['taker_buy_share']<RULES['min_taker_buy_share'] or fr['volume_expansion']<RULES['min_relative_volume'] or not(fr['breakout'] or fr['reclaim']) or opp_rank[sym]>TOP_N or fr['r24']>RULES['max_positive_24h_extension']:continue
            if sym in last and i-last[sym]<MIN_GAP_BARS:continue
            row={'event_id':f'HIST-{sym}-{ts}','source_ts':ts,'ts':ts,'symbol':sym,'lane':lane(fr['r24']),'regime':rg,'score':fr['score'],'risk_pct':fr['risk_pct'],'reward_pct':fr['reward_pct'],'breadth_1h':b1,'breadth_4h':b4,'rs_vs_btc_4h':fr['rs_vs_btc_4h'],'r1h_pct_rank':ranks['r1h'][sym],'r4h_pct_rank':ranks['r4h'][sym],'rs_btc4h_pct_rank':ranks['rs_vs_btc_4h'][sym],'accel1h_pct_rank':ranks['accel1h'][sym],'volume_expansion_pct_rank':ranks['volume_expansion'][sym],'liquidity_pct_rank':ranks['liquidity'][sym],'opportunity_pct_shadow':opp_pct[sym],'opportunity_rank':opp_rank[sym],'risk_reward':{'risk':fr['risk_pct'],'reward':fr['reward_pct']}}
            o=outcome(data[sym],i,row)
            if o:events.append(o);last[sym]=i
    events.sort(key=lambda r:(r['source_ts'],r['symbol'])); rows=[r for r in events if r.get('tp_before_sl') in {0,1}]
    base={'version':1,'authorization':'RESEARCH_ONLY','liveTrading':False,'generated_at':time.time(),'source':'Binance Vision Spot monthly 15m archives','feature_version':m.FEATURE_VERSION,'ruleset_hash':RULESET_HASH,'rules':RULES,'months':mos,'symbols_requested':SYMBOLS,'symbols_with_data':[s for s in SYMBOLS if data.get(s)],'download_failures':failures,'candidate_events':len(events),'path_labeled_events':len(rows),'minimum_historical_sample':MIN_HIST,'survivorship_bias_note':'Frozen listed-symbol universe; historical approval is never sufficient without fresh forward confirmation.','status':'NOT_APPROVED','evidence_pass':False,'model':{}}
    if len(rows)<MIN_HIST:
        base['status']='INSUFFICIENT_HISTORICAL_SAMPLE';OUT.write_text(json.dumps(base,indent=2,sort_keys=True));print(json.dumps({'kind':'historical_ev_replay','status':base['status'],'n':len(rows)}));return
    n=len(rows); ntr=int(n*.60); ncal=int(n*.20); tr=rows[:ntr+ncal]; te=rows[ntr+ncal:]; p,e=fit_predict(tr,te); chrono=metric(te,p,e); prob=m._prob_metrics(p,[int(r['tp_before_sl']) for r in te]) if p else {'n':0}
    initial=max(240,int(n*.40)); step=max(MIN_TEST,(n-initial)//4); folds=[]; cur=initial
    while cur<n and len(folds)<4:
        end=n if len(folds)==3 else min(n,cur+step); test=rows[cur:end]
        if len(test)<MIN_TEST:break
        pp,ee=fit_predict(rows[:cur],test); fm=metric(test,pp,ee);fm.update({'train_n':cur,'start_ts':test[0]['source_ts'],'end_ts':test[-1]['source_ts']});folds.append(fm);cur=end
    hold_syms={s for s in {r['symbol'] for r in rows} if int(hashlib.sha256(s.encode()).hexdigest()[:8],16)%5==0}; train=[r for r in rows if r['symbol'] not in hold_syms]; hold=[r for r in rows if r['symbol'] in hold_syms]; hp,he=fit_predict(train,hold); hm=metric(hold,hp,he);hm['symbols']=sorted(hold_syms)
    fold_pass=len(folds)>=3 and all(bool(x.get('pass')) for x in folds); bs=float(prob.get('brier_skill') or -1); cm=float(prob.get('calibration_mae') or 99); calpass=bs>.02 and cm<=.10; holdpass=len(hold)>=80 and len(hold_syms)>=3 and bool(hm.get('pass')); approved=bool(chrono.get('pass')) and fold_pass and holdpass and calpass
    base.update({'status':'APPROVED' if approved else 'NOT_APPROVED','evidence_pass':approved,'chronological_oos':chrono,'chronological_probability':prob,'walk_forward_folds':folds,'walk_forward_pass':fold_pass,'unseen_symbol_holdout':hm,'unseen_symbol_holdout_pass':holdpass,'calibration_pass':calpass,'model':build_model(rows) if approved else {}});OUT.write_text(json.dumps(base,indent=2,sort_keys=True));print(json.dumps({'kind':'historical_ev_replay','status':base['status'],'candidates':len(events),'path':len(rows),'folds':len(folds),'holdout_symbols':len(hold_syms),'feature_version':m.FEATURE_VERSION,'ruleset_hash':RULESET_HASH},separators=(',',':')))

if __name__=='__main__':main()
