import json
import pathlib
import numpy as np
from jesse.strategies import Strategy

MANIFEST_PATH=pathlib.Path("validation/fusion/candidate-manifest.json")
MANIFEST=json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {}
FAMILY=MANIFEST.get("family","TS_MOMENTUM")
PARAMS=MANIFEST.get("params") or {}
TF=MANIFEST.get("timeframe","1h")
LEADER_PATH=pathlib.Path("validation/fusion/jesse-leader.json")
WARMUP_BARS=800
_LEADER_CACHE=None

def leader_value(ts):
    global _LEADER_CACHE
    if FAMILY!="CROSS_CRYPTO_LEAD_LAG":
        return 0.0
    if _LEADER_CACHE is None:
        try:_LEADER_CACHE=json.loads(LEADER_PATH.read_text())
        except Exception:_LEADER_CACHE={}
    try:return float(_LEADER_CACHE.get(str(int(float(ts))),0.0))
    except Exception:return 0.0

def ema(values,n):
    x=np.asarray(values,dtype=float)
    if len(x)<n:return None
    a=2/(n+1);e=float(x[0])
    for v in x[1:]:e=a*float(v)+(1-a)*e
    return float(e)

def rsi_wilder(values,n=14):
    x=np.asarray(values,dtype=float)
    if len(x)<n+1:return 50.0
    d=np.diff(x)
    gains=np.clip(d,0,None);losses=np.clip(-d,0,None)
    ag=float(gains[0]);al=float(losses[0]);alpha=1/n
    for g,l in zip(gains[1:],losses[1:]):
        ag=alpha*float(g)+(1-alpha)*ag
        al=alpha*float(l)+(1-alpha)*al
    if al<=1e-15:return 100.0
    rs=ag/al
    return float(100-100/(1+rs))

def atr_pct(candles,n=14):
    if len(candles)<n+1:return 0.0
    vals=[]
    for i in range(len(candles)-n,len(candles)):
        prev=float(candles[i-1,2]);h=float(candles[i,3]);l=float(candles[i,4])
        vals.append(max(h-l,abs(h-prev),abs(l-prev)))
    close=float(candles[-1,2])
    return (sum(vals)/len(vals))/close if close>0 else 0.0

class UnifiedCandidateValidator(Strategy):
    STRATEGY_ID="TST_CANDIDATE_JESSE_VALIDATOR_V1"

    def should_long(self)->bool:
        p=PARAMS;c=self.candles
        needed=max(WARMUP_BARS,int(p.get("zLookback",0))+10)
        if len(c)<needed:return False
        closes=c[:,2].astype(float);highs=c[:,3].astype(float);volumes=c[:,5].astype(float)
        close=float(closes[-1]);R=rsi_wilder(closes)
        qv=volumes*closes
        med=float(np.median(qv[-24:])) if len(qv)>=24 else 0.0
        rel=float(qv[-1]/med) if med>0 else 0.0

        if FAMILY=="CROSS_CRYPTO_LEAD_LAG":
            lead=leader_value(self.current_candle[0]);alt3=float(close/closes[-4]-1);gap=lead-alt3
            e24=ema(closes,int(p.get("emaFast",24)))
            return bool(e24 and lead>=float(p.get("leaderRetMin",0.012)) and gap>=float(p.get("gapMin",0.008)) and alt3>float(p.get("altRetMin",-0.02)) and close>e24 and rel>=float(p.get("relvol",0.9)) and float(p.get("rsiMin",42))<=R<=float(p.get("rsiMax",70)))

        if FAMILY=="TS_MOMENTUM":
            ef=ema(closes,int(p.get("emaFast",48)));es=ema(closes,int(p.get("emaSlow",120)))
            lb=int(p.get("retLookback",24));ret=close/float(closes[-lb-1])-1;a=atr_pct(c)
            return bool(ef and es and close>ef>es and ret>float(p.get("retMin",0.02)) and float(p.get("atrMin",0.006))<=a<=float(p.get("atrMax",0.08)) and rel>=float(p.get("relvol",0.8)))

        if FAMILY=="LIQUIDITY_REVERSAL":
            lb=int(p.get("retLookback",6));zlb=int(p.get("zLookback",720));rets=[]
            for i in range(len(closes)-zlb,len(closes)):
                if i-lb>=0:rets.append(float(closes[i]/closes[i-lb]-1))
            if len(rets)<100:return False
            mu=float(np.mean(rets));sd=float(np.std(rets));cur=float(close/closes[-lb-1]-1);z=(cur-mu)/sd if sd>0 else 0
            week=qv[-7*24:];vr=float(qv[-1]/np.median(week)) if len(week) and np.median(week)>0 else 999;e24=ema(closes,24)
            return bool(z<=float(p.get("zMax",-2)) and vr<=float(p.get("volumeRatioMax",1.1)) and R<=float(p.get("rsiMax",35)) and e24 and close<e24)

        if FAMILY=="VOLATILITY_BREAKOUT":
            lb=int(p.get("lookback",24));hh=float(np.max(highs[-lb-1:-1]));samples=[];clb=int(p.get("compressionLookback",72))
            for off in range(clb,0,-1):
                sub=c[:-off] if off>0 else c
                if len(sub)>=20:samples.append(atr_pct(sub))
            cur=atr_pct(c);pct=(sum(1 for x in samples if x<=cur)/len(samples)) if samples else 1
            return bool(pct<=float(p.get("compressionPct",0.25)) and close>hh and rel>=float(p.get("relvol",1.5)) and float(p.get("rsiMin",55))<=R<=float(p.get("rsiMax",75)))

        if FAMILY=="TREND_BREAKOUT":
            ef=ema(closes,int(p.get("fast",20)));es=ema(closes,int(p.get("slow",60)));hh=float(np.max(highs[-int(p.get("lookback",20))-1:-1]))
            return bool(ef and es and ef>es and close>hh and rel>=float(p.get("relvol",1.0)) and 52<=R<=72)

        if FAMILY=="MEAN_REVERSION":
            x=closes[-20:];mid=float(np.mean(x));sd=float(np.std(x));lower=mid-float(p.get("bb",2))*sd
            return bool(close<lower and R<=float(p.get("rsi_in",34)) and rel>=0.75)

        if FAMILY=="VOLATILITY_MOMENTUM":
            hh=float(np.max(highs[-int(p.get("lookback",20))-1:-1]));e20=ema(closes,20)
            return bool(e20 and close>hh and rel>=float(p.get("relvol",1.4)) and float(p.get("rsi_min",52))<=R<=74 and close>e20)
        return False

    def should_short(self)->bool:return False

    def go_long(self):
        entry=float(self.price);size_usd=min(5.5,max(0,float(self.balance)));qty=max(size_usd/entry,1e-8)
        sl=float(PARAMS.get("sl",0.03));tp=float(PARAMS.get("tp",0.06))
        self.buy=qty,entry;self.stop_loss=qty,entry*(1-sl);self.take_profit=qty,entry*(1+tp)
        try:self.vars["entry_ts"]=float(self.current_candle[0])
        except Exception:pass

    def update_position(self):
        hold=int(PARAMS.get("holdBars",0))
        if hold<=0 or not self.is_long:return
        mins=60 if TF=="1h" else 15
        try:
            entry_ts=float(self.vars.get("entry_ts",self.current_candle[0]))
            if float(self.current_candle[0])-entry_ts>=hold*mins*60*1000:self.liquidate()
        except Exception:pass

    def go_short(self):pass
    def should_cancel_entry(self)->bool:return True
