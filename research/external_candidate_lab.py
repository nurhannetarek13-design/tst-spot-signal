#!/usr/bin/env python3
"""Research-only external candidate lab for Binance Spot.
Canonical reimplementations inspired by public strategy families. No live authorization.
"""
import datetime as dt
import json, math, pathlib, time, urllib.parse, urllib.request
import numpy as np
import pandas as pd

BASE_URL="https://data-api.binance.vision"
INTERVAL="1h"; DAYS=365; MAX_SYMBOLS=40
MIN_QV=20_000_000; MAX_QV=200_000_000; MAX_PRICE=5.0
STAKE=5.5; BASE_COST=0.0015; STRESS_COST=0.0030
OUT=pathlib.Path("validation/external-candidates/external-candidate-lab-latest.json")
STRATEGY_ID="TST_EXTERNAL_CANONICAL_V1"
MAJORS={"BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","LTC","BCH","LINK","AVAX","DOT"}
EXCLUDED={"USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","AEUR","TRY","BRL","GBP","AUD","USD1","RLUSD","USDE","PAXG","XAUT"}
PROVENANCE={
 "HYBRID_REGIME":"TradingView Hybrid RSI + Breakout concept; independently reimplemented",
 "TRENDSHIFT_ADX":"TradingView regime-adaptive Supertrend/ADX concept; independently reimplemented",
 "PIVOT_RSI":"TradingView Pivot Point Reversal + RSI concept; independently reimplemented",
 "DWT_FORECAST":"nateemma DWT/TSPredict family concept; independent causal wavelet-style forecast proxy",
 "RSI_BB_PANIC_REVERSION":"public RSI/Bollinger panic-reversion concept; independently reimplemented"
}

def api(path):
    req=urllib.request.Request(BASE_URL+path,headers={"User-Agent":"tst-external-candidate-lab/1.0"})
    with urllib.request.urlopen(req,timeout=30) as r: return json.load(r)

def universe():
    info=api('/api/v3/exchangeInfo'); tick={x['symbol']:x for x in api('/api/v3/ticker/24hr')}; rows=[]
    for s in info.get('symbols',[]):
        if s.get('status')!='TRADING' or s.get('quoteAsset')!='USDT' or not s.get('isSpotTradingAllowed'): continue
        b=s.get('baseAsset','')
        if not b or b in EXCLUDED or b in MAJORS or b.endswith(('UP','DOWN','BULL','BEAR')): continue
        t=tick.get(s['symbol'],{}); px=float(t.get('lastPrice') or 0); qv=float(t.get('quoteVolume') or 0)
        if 0<px<=MAX_PRICE and MIN_QV<=qv<=MAX_QV: rows.append({'symbol':s['symbol'],'price':px,'quoteVolume24h':qv})
    rows.sort(key=lambda x:x['quoteVolume24h'], reverse=True); return rows[:MAX_SYMBOLS]

def klines(symbol):
    end=int(time.time()*1000); start=end-DAYS*86400000; cur=start; rows=[]
    while cur<end:
        q=urllib.parse.urlencode({'symbol':symbol,'interval':INTERVAL,'limit':1000,'startTime':cur,'endTime':end})
        batch=api('/api/v3/klines?'+q)
        if not batch: break
        rows.extend(batch); nxt=int(batch[-1][0])+3600000
        if nxt<=cur: break
        cur=nxt; time.sleep(0.01)
    if len(rows)<4000: raise RuntimeError(f'{symbol}: insufficient bars {len(rows)}')
    df=pd.DataFrame(rows,columns=['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_base','taker_quote','ignore'])
    for c in ['open','high','low','close','volume','quote_volume','taker_quote']: df[c]=pd.to_numeric(df[c],errors='coerce')
    df['ts']=pd.to_datetime(df['open_time'],unit='ms',utc=True)
    return df.set_index('ts')[['open','high','low','close','volume','quote_volume','taker_quote']].dropna()

def ema(s,n): return s.ewm(span=n,adjust=False).mean()
def rsi(s,n=14):
    d=s.diff(); g=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean(); l=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    return (100-100/(1+g/l.replace(0,np.nan))).fillna(50)
def atr(df,n=14):
    pc=df.close.shift(1); tr=pd.concat([(df.high-df.low).abs(),(df.high-pc).abs(),(df.low-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()
def adx(df,n=14):
    up=df.high.diff(); dn=-df.low.diff(); plus=np.where((up>dn)&(up>0),up,0.0); minus=np.where((dn>up)&(dn>0),dn,0.0)
    a=atr(df,n).replace(0,np.nan); p=100*pd.Series(plus,index=df.index).rolling(n).sum()/a.rolling(n).sum(); m=100*pd.Series(minus,index=df.index).rolling(n).sum()/a.rolling(n).sum()
    return (100*(p-m).abs()/(p+m).replace(0,np.nan)).rolling(n).mean().fillna(0)

def supertrend_proxy(df,mult):
    a=atr(df,14); mid=(df.high+df.low)/2; upper=mid+mult*a; lower=mid-mult*a
    trend=pd.Series(False,index=df.index); state=False
    for i in range(1,len(df)):
        if df.close.iloc[i]>upper.iloc[i-1]: state=True
        elif df.close.iloc[i]<lower.iloc[i-1]: state=False
        trend.iloc[i]=state
    return trend

def dwt_proxy_signal(c):
    # Causal multiscale forecast proxy: weighted low-frequency returns only from past bars.
    r=c.pct_change(); slow=r.rolling(16).mean(); mid=r.rolling(8).mean(); fast=r.rolling(4).mean()
    forecast=0.5*slow+0.3*mid+0.2*fast
    return forecast

def signals(df,fam):
    c=df.close; qv=df.quote_volume; rv=qv/qv.rolling(24).median().replace(0,np.nan); R=rsi(c); A=adx(df); E50=ema(c,50); E200=ema(c,200)
    if fam=='HYBRID_REGIME':
        range_mode=A<20; trend_mode=A>=20; bbm=c.rolling(20).mean(); bbs=c.rolling(20).std(); lower=bbm-2*bbs; hh=df.high.rolling(24).max().shift(1)
        sig=(range_mode&(c<lower)&(R<30)&(rv>=0.8)) | (trend_mode&(c>hh)&(c>E200)&(R.between(50,75))&(rv>=1.3))
        return sig.fillna(False),{'hold':12,'sl':0.028,'tp':0.055}
    if fam=='TRENDSHIFT_ADX':
        mult=pd.Series(np.where(A>=30,2.0,np.where(A>=20,2.5,3.2)),index=df.index); st=supertrend_proxy(df,float(mult.dropna().median() if len(mult.dropna()) else 2.5))
        sig=st&(A>=20)&(c>E50)&(E50>E200)&(rv>=0.9)
        return sig.fillna(False),{'hold':18,'sl':0.03,'tp':0.065}
    if fam=='PIVOT_RSI':
        pivot_low=(df.low.shift(2)>df.low.shift(1))&(df.low>df.low.shift(1)); recover=c>df.high.shift(1)
        sig=pivot_low&recover&(R.shift(1)<35)&(rv>=0.75)
        return sig.fillna(False),{'hold':10,'sl':0.025,'tp':0.045}
    if fam=='DWT_FORECAST':
        f=dwt_proxy_signal(c); sig=(f>0.0015)&(f.shift(1)<=0.0015)&(c>E50)&(R.between(45,72))&(rv>=0.8)
        return sig.fillna(False),{'hold':12,'sl':0.025,'tp':0.05}
    if fam=='RSI_BB_PANIC_REVERSION':
        m=c.rolling(20).mean(); s=c.rolling(20).std(); lower=m-2.2*s; ret6=c.pct_change(6)
        sig=(c<lower)&(R<28)&(ret6<-0.035)&(rv>=1.1)
        return sig.fillna(False),{'hold':12,'sl':0.03,'tp':0.05}
    raise ValueError(fam)

def backtest(df,sig,rules,cost):
    trades=[]; i=0; n=len(df)
    while i<n-2:
        if not bool(sig.iloc[i]): i+=1; continue
        ei=i+1; entry=float(df.open.iloc[ei]); stop=entry*(1-rules['sl']); target=entry*(1+rules['tp']); xi=min(n-1,ei+rules['hold']); xp=float(df.close.iloc[xi]); reason='TIME'
        for j in range(ei,xi+1):
            if float(df.low.iloc[j])<=stop: xi=j; xp=stop; reason='STOP'; break
            if float(df.high.iloc[j])>=target: xi=j; xp=target; reason='TARGET'; break
        gross=STAKE*(xp/entry-1); fees=STAKE*cost+(STAKE*(xp/entry))*cost
        trades.append({'exit':df.index[xi].isoformat(),'pnl':gross-fees,'reason':reason}); i=xi+1
    return trades

def metrics(trades):
    p=np.array([t['pnl'] for t in trades],float)
    if len(p)==0:return {'trades':0,'winRate':0.0,'netPnlUSDT':0.0,'expectancyUSDT':0.0,'profitFactor':0.0,'maxDrawdownUSDT':0.0}
    gp=float(p[p>0].sum()) if np.any(p>0) else 0.; gl=float(-p[p<0].sum()) if np.any(p<0) else 0.; eq=np.cumsum(p); peak=np.maximum.accumulate(np.r_[0.,eq])[:-1]
    return {'trades':int(len(p)),'winRate':float((p>0).mean()),'netPnlUSDT':float(p.sum()),'expectancyUSDT':float(p.mean()),'profitFactor':float(gp/gl) if gl>0 else (999. if gp>0 else 0.),'maxDrawdownUSDT':float(np.maximum(0,peak-eq).max(initial=0.0))}

def gate(base,stress):
    return bool(base['trades']>=40 and base['expectancyUSDT']>0 and base['profitFactor']>=1.15 and stress['expectancyUSDT']>0 and stress['profitFactor']>=1.0 and base['maxDrawdownUSDT']<=2.0)

rows=universe(); data={}; failures={}
for x in rows:
    try:data[x['symbol']]=klines(x['symbol'])
    except Exception as e:failures[x['symbol']]=str(e)

families=list(PROVENANCE); results=[]; per_symbol=[]
for fam in families:
    pb=[]; ps=[]
    for x in rows:
        s=x['symbol']; df=data.get(s)
        if df is None: continue
        # fixed rules: final 40% only, first 60% untouched warmup/history
        sub=df.iloc[int(len(df)*0.60):].copy(); sig,rules=signals(sub,fam)
        tb=backtest(sub,sig,rules,BASE_COST); ts=backtest(sub,sig,rules,STRESS_COST); mb,ms=metrics(tb),metrics(ts); pb.extend(tb); ps.extend(ts)
        if mb['trades']>=5: per_symbol.append({'family':fam,'symbol':s,'base':mb,'stress2x':ms})
    b=metrics(pb); st=metrics(ps); results.append({'family':fam,'provenance':PROVENANCE[fam],'base':b,'stress2x':st,'edgePass':gate(b,st)})
results.sort(key=lambda x:(x['edgePass'],x['stress2x']['expectancyUSDT'],x['base']['profitFactor']),reverse=True)
report={'engine':'EXTERNAL_CANONICAL_LAB','strategyId':STRATEGY_ID,'authorization':'RESEARCH_ONLY','liveTrading':False,'status':'EDGE_FOUND' if any(x['edgePass'] for x in results) else 'NO_EDGE_PASS','pass':any(x['edgePass'] for x in results),'families':results,'topSymbolResults':sorted(per_symbol,key=lambda x:(x['stress2x']['expectancyUSDT'],x['base']['profitFactor']),reverse=True)[:30],'universe':rows,'costs':{'basePerSide':BASE_COST,'stressPerSide':STRESS_COST},'split':{'warmup':0.60,'evaluation':0.40},'dataFailures':failures,'generatedAt':dt.datetime.now(dt.timezone.utc).isoformat(),'notes':['Independent canonical reimplementations; not copied Pine/Freqtrade code.','Signals enter next-bar open.','Same-bar stop/target collision assumes stop first.','A pass authorizes further validation only, never live trading.']}
OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(report,indent=2)); print(json.dumps(report,indent=2))
