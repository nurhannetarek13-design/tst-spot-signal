#!/usr/bin/env python3
import datetime as dt, json, pathlib, time, urllib.parse, urllib.request
import numpy as np, pandas as pd, vectorbt as vbt

MANIFEST=pathlib.Path("validation/fusion/candidate-manifest.json")
OUT=pathlib.Path("validation/fusion/vectorbt-candidate-latest.json")
DAYS=365
INIT=20.08
STAKE=5.5
BASE_FEE=0.0015
STRESS_FEE=0.003
STRATEGY_ID="TST_CANDIDATE_VECTORBT_VALIDATOR_V1"

def fetch(symbol,tf):
    end=int(time.time()*1000); start=end-DAYS*86400000
    step={"15m":900000,"1h":3600000}[tf]
    rows=[]; cur=start
    while cur<end:
        qs=urllib.parse.urlencode({"symbol":symbol,"interval":tf,"limit":1000,"startTime":cur,"endTime":end})
        req=urllib.request.Request("https://data-api.binance.vision/api/v3/klines?"+qs,headers={"User-Agent":"tst-candidate-vectorbt/1.0"})
        with urllib.request.urlopen(req,timeout=25) as r: batch=json.load(r)
        if not batch: break
        rows.extend(batch); nxt=int(batch[-1][0])+step
        if nxt<=cur: break
        cur=nxt; time.sleep(0.01)
    df=pd.DataFrame(rows,columns=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"])
    for c in ["open","high","low","close","volume","quote_volume","taker_quote"]: df[c]=pd.to_numeric(df[c],errors="coerce")
    df["ts"]=pd.to_datetime(df["open_time"],unit="ms",utc=True)
    return df.set_index("ts")[["open","high","low","close","volume","quote_volume","taker_quote"]].dropna()

def ema(s,n): return s.ewm(span=n,adjust=False).mean()
def rsi(s,n=14):
    d=s.diff(); g=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean(); l=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    rs=g/l.replace(0,np.nan); return (100-100/(1+rs)).fillna(50)
def atrp(df,n=14):
    pc=df.close.shift(1)
    tr=pd.concat([(df.high-df.low).abs(),(df.high-pc).abs(),(df.low-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()/df.close

def raw_signal(df,fam,p):
    c=df.close; rv=df.quote_volume/df.quote_volume.rolling(24).median().replace(0,np.nan); R=rsi(c)
    if fam=="TS_MOMENTUM":
        return (c>ema(c,p["emaFast"]))&(ema(c,p["emaFast"])>ema(c,p["emaSlow"]))&(c.pct_change(p["retLookback"])>p["retMin"])&atrp(df).between(p["atrMin"],p["atrMax"])&(rv>=p["relvol"])
    if fam=="LIQUIDITY_REVERSAL":
        rr=c.pct_change(p["retLookback"]); mu=rr.rolling(p["zLookback"]).mean(); sd=rr.rolling(p["zLookback"]).std(ddof=0).replace(0,np.nan); z=(rr-mu)/sd
        vr=df.quote_volume/df.quote_volume.rolling(7*24).median().replace(0,np.nan)
        return (z<=p["zMax"])&(vr<=p["volumeRatioMax"])&(R<=p["rsiMax"])&(c<ema(c,24))
    if fam=="VOLATILITY_BREAKOUT":
        a=atrp(df); rank=a.rolling(p["compressionLookback"]).rank(pct=True); hh=df.high.rolling(p["lookback"]).max().shift(1)
        return (rank.shift(1)<=p["compressionPct"])&(c>hh)&(rv>=p["relvol"])&R.between(p["rsiMin"],p["rsiMax"])
    if fam=="TREND_BREAKOUT":
        f=ema(c,p["fast"]); s=ema(c,p["slow"]); hh=df.high.rolling(p["lookback"]).max().shift(1)
        return (f>s)&(c>hh)&(rv>=p["relvol"])&R.between(52,72)
    if fam=="MEAN_REVERSION":
        mid=c.rolling(20).mean(); sd=c.rolling(20).std(ddof=0); lower=mid-p["bb"]*sd
        return (c<lower)&(R<=p["rsi_in"])&(rv>=0.75)
    if fam=="VOLATILITY_MOMENTUM":
        hh=df.high.rolling(p["lookback"]).max().shift(1); e20=ema(c,20)
        return (c>hh)&(rv>=p["relvol"])&R.between(p["rsi_min"],74)&(c>e20)
    raise ValueError(f"unsupported family {fam}")

def build_signals(df,raw,hold):
    raw=raw.fillna(False).to_numpy(bool)
    entries=np.zeros(len(df),dtype=bool); exits=np.zeros(len(df),dtype=bool)
    i=1
    while i<len(df)-1:
        # signal is known only after bar i closes; enter next bar
        if raw[i]:
            e=i+1; x=min(len(df)-1,e+hold)
            entries[e]=True; exits[x]=True; i=x+1
        else: i+=1
    return pd.Series(entries,index=df.index),pd.Series(exits,index=df.index)

def metrics(pf):
    rr=pf.trades.closed.records_readable
    if rr.empty:return {"trades":0,"wins":0,"winRate":0.0,"netPnlUSDT":0.0,"expectancyUSDT":0.0,"profitFactor":0.0,"maxDrawdownUSDT":0.0}
    p=rr["PnL"].astype(float).to_numpy(); gp=p[p>0].sum() if np.any(p>0) else 0; gl=-p[p<0].sum() if np.any(p<0) else 0
    eq=np.cumsum(p); peak=np.maximum.accumulate(np.r_[0.0,eq])[:-1]; dd=peak-eq
    return {"trades":int(len(p)),"wins":int((p>0).sum()),"winRate":float((p>0).mean()),"netPnlUSDT":float(p.sum()),"expectancyUSDT":float(p.mean()),"profitFactor":float(gp/gl) if gl>0 else (999.0 if gp>0 else 0.0),"maxDrawdownUSDT":float(max(0.0,dd.max(initial=0.0)))}

m=json.loads(MANIFEST.read_text())
if not m.get("candidateFingerprint"):
    out={"engine":"VECTORBT_CANDIDATE","strategyId":STRATEGY_ID,"status":"NO_CANDIDATE","pass":False,"candidateFingerprint":None,"liveTrading":False,"generatedAt":dt.datetime.now(dt.timezone.utc).isoformat()}
else:
    df=fetch(m["symbol"],m["timeframe"]); p=m["params"]; hold=int(p.get("holdBars",24 if m["timeframe"]=="1h" else 96))
    raw=raw_signal(df,m["family"],p); entries,exits=build_signals(df,raw,hold)
    start=int(len(df)*0.60)
    def run(fee):
        sub=df.iloc[start:]; e=entries.iloc[start:]; x=exits.iloc[start:]
        pf=vbt.Portfolio.from_signals(sub.close,e,x,init_cash=INIT,size=STAKE,size_type="value",fees=fee,sl_stop=float(p["sl"]),tp_stop=float(p["tp"]),direction="longonly",freq=m["timeframe"])
        return metrics(pf)
    base=run(BASE_FEE); stress=run(STRESS_FEE)
    independent=base["trades"]>=30 and base["profitFactor"]>=1.15 and base["expectancyUSDT"]>0 and stress["profitFactor"]>=1.0 and stress["expectancyUSDT"]>0 and base["maxDrawdownUSDT"]<=2.0
    passed=independent and base["trades"]>=100 and stress["trades"]>=100
    out={"engine":"VECTORBT_CANDIDATE","strategyId":STRATEGY_ID,"status":"PASS" if passed else "FAIL","pass":passed,"independentEnginePass":independent,"candidateId":m["candidateId"],"candidateFingerprint":m["candidateFingerprint"],"symbol":m["symbol"],"family":m["family"],"timeframe":m["timeframe"],"params":p,"base":base,"stress2x":stress,"authorization":"RESEARCH_ONLY","liveTrading":False,"generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),"notes":"Same unified candidate; next-bar entry; long-only; 5.5 USDT; realistic base and doubled friction."}
OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2)); print(json.dumps(out,indent=2))
