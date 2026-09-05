#!/usr/bin/env python3
import datetime as dt, json, pathlib, time, urllib.parse, urllib.request
import numpy as np, pandas as pd

MANIFEST=pathlib.Path("validation/fusion/frozen-parity-candidate.json")
FIXTURE=pathlib.Path("validation/fusion/execution-parity-fixture.json")
OUT=pathlib.Path("validation/fusion/vectorbt-candidate-latest.json")
DAYS=365
WARMUP=800
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
        req=urllib.request.Request("https://data-api.binance.vision/api/v3/klines?"+qs,headers={"User-Agent":"tst-vectorbt-parity/1.0"})
        with urllib.request.urlopen(req,timeout=25) as r: batch=json.load(r)
        if not batch: break
        rows.extend(batch); nxt=int(batch[-1][0])+step
        if nxt<=cur: break
        cur=nxt; time.sleep(0.01)
    df=pd.DataFrame(rows,columns=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"])
    for c in ["open","high","low","close","volume","quote_volume"]: df[c]=pd.to_numeric(df[c],errors="coerce")
    df["ts"]=pd.to_datetime(df["open_time"],unit="ms",utc=True)
    return df.set_index("ts")[["open","high","low","close","volume","quote_volume"]].dropna()


def ema(s,n): return s.ewm(span=int(n),adjust=False).mean()
def rsi(s,n=14):
    d=s.diff(); g=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean(); l=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    rs=g/l.replace(0,np.nan); return (100-100/(1+rs)).fillna(50.0)
def atrp(df,n=14):
    pc=df.close.shift(1)
    tr=pd.concat([(df.high-df.low).abs(),(df.high-pc).abs(),(df.low-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()/df.close


def raw_signal(df,fam,p,leader=None):
    c=df.close
    qv=df.volume*df.close
    rv=qv/qv.rolling(24).median().replace(0,np.nan)
    R=rsi(c)
    if fam=="CROSS_CRYPTO_LEAD_LAG":
        if leader is None:return pd.Series(False,index=df.index)
        lead=leader.reindex(df.index).ffill(); alt3=c.pct_change(3); gap=lead-alt3
        return (lead>=float(p.get("leaderRetMin",0.012)))&(gap>=float(p.get("gapMin",0.008)))&(alt3>float(p.get("altRetMin",-0.02)))&(c>ema(c,int(p.get("emaFast",24))))&(rv>=float(p.get("relvol",0.9)))&R.between(float(p.get("rsiMin",42)),float(p.get("rsiMax",70)))
    if fam=="TS_MOMENTUM":
        return (c>ema(c,p["emaFast"]))&(ema(c,p["emaFast"])>ema(c,p["emaSlow"]))&(c.pct_change(p["retLookback"])>p["retMin"])&atrp(df).between(p["atrMin"],p["atrMax"])&(rv>=p["relvol"])
    if fam=="LIQUIDITY_REVERSAL":
        rr=c.pct_change(p["retLookback"]); mu=rr.rolling(p["zLookback"]).mean(); sd=rr.rolling(p["zLookback"]).std(ddof=0).replace(0,np.nan); z=(rr-mu)/sd
        vr=qv/qv.rolling(7*24).median().replace(0,np.nan)
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


def simulate(df,signal,p,fee):
    hold=int(p.get("holdBars",0)); sl=float(p.get("sl",0.03)); tp=float(p.get("tp",0.06))
    trades=[]; i=WARMUP
    while i<len(df)-1:
        if not bool(signal.iloc[i]): i+=1; continue
        e=i+1; entry=float(df.open.iloc[e]); stop=entry*(1-sl); target=entry*(1+tp)
        x=min(len(df)-1,e+max(hold,1)); exit_price=float(df.close.iloc[x]); reason="TIME"
        for j in range(e,x+1):
            lo=float(df.low.iloc[j]); hi=float(df.high.iloc[j]); hit_sl=lo<=stop; hit_tp=hi>=target
            if hit_sl and hit_tp: x,exit_price,reason=j,stop,"SL_AMBIGUOUS_CONSERVATIVE"; break
            if hit_sl: x,exit_price,reason=j,stop,"SL"; break
            if hit_tp: x,exit_price,reason=j,target,"TP"; break
        qty=STAKE/entry; pnl=qty*(exit_price-entry)-STAKE*fee-qty*exit_price*fee
        trades.append({"signalTs":df.index[i].isoformat(),"entryTs":df.index[e].isoformat(),"exitTs":df.index[x].isoformat(),"entryPrice":entry,"exitPrice":exit_price,"reason":reason,"pnlUSDT":pnl})
        i=x+1
    return trades


def metrics(trades):
    if not trades:return {"trades":0,"wins":0,"winRate":0.0,"netPnlUSDT":0.0,"expectancyUSDT":0.0,"profitFactor":0.0,"maxDrawdownUSDT":0.0}
    p=np.asarray([x["pnlUSDT"] for x in trades],dtype=float); gp=p[p>0].sum() if np.any(p>0) else 0.0; gl=-p[p<0].sum() if np.any(p<0) else 0.0
    eq=np.cumsum(p); peak=np.maximum.accumulate(np.r_[0.0,eq])[:-1]; dd=peak-eq
    return {"trades":int(len(p)),"wins":int((p>0).sum()),"winRate":float((p>0).mean()),"netPnlUSDT":float(p.sum()),"expectancyUSDT":float(p.mean()),"profitFactor":float(gp/gl) if gl>0 else (999.0 if gp>0 else 0.0),"maxDrawdownUSDT":float(max(0.0,dd.max(initial=0.0)))}

m=json.loads(MANIFEST.read_text())
if not m.get("candidateFingerprint"):
    out={"engine":"VECTORBT_CANDIDATE","strategyId":STRATEGY_ID,"status":"NO_CANDIDATE","pass":False,"candidateFingerprint":None,"liveTrading":False,"generatedAt":dt.datetime.now(dt.timezone.utc).isoformat()}
else:
    df=fetch(m["symbol"],m["timeframe"]); p=m["params"]
    leader=None
    if m["family"]=="CROSS_CRYPTO_LEAD_LAG":
        anchors=[]
        for s in ["BTCUSDT","ETHUSDT","SOLUSDT"]:
            adf=fetch(s,m["timeframe"]); anchors.append(adf["close"].pct_change(3).rename(s))
        leader=pd.concat(anchors,axis=1).mean(axis=1)
    raw=raw_signal(df,m["family"],p,leader).fillna(False); raw.iloc[:WARMUP]=False
    bt=simulate(df,raw,p,BASE_FEE); st=simulate(df,raw,p,STRESS_FEE)
    base=metrics(bt); stress=metrics(st)
    fixture=json.loads(FIXTURE.read_text()) if FIXTURE.exists() else {}
    fixture_match=(fixture.get("candidateFingerprint")==m.get("candidateFingerprint") and base.get("trades")==fixture.get("base",{}).get("trades") and abs(base.get("profitFactor",0)-fixture.get("base",{}).get("profitFactor",0))<1e-9 and abs(stress.get("profitFactor",0)-fixture.get("stress2x",{}).get("profitFactor",0))<1e-9)
    independent=base["trades"]>=30 and base["profitFactor"]>=1.15 and base["expectancyUSDT"]>0 and stress["profitFactor"]>=1.0 and stress["expectancyUSDT"]>0 and base["maxDrawdownUSDT"]<=2.0
    passed=independent and base["trades"]>=100 and stress["trades"]>=100
    out={"engine":"VECTORBT_CANDIDATE","strategyId":STRATEGY_ID,"status":"PASS" if passed else "FAIL","pass":passed,"independentEnginePass":independent,"candidateId":m["candidateId"],"candidateFingerprint":m["candidateFingerprint"],"symbol":m["symbol"],"family":m["family"],"timeframe":m["timeframe"],"params":p,"paritySpecVersion":"TST_EXECUTION_PARITY_V1","parityFixtureMatch":fixture_match,"base":base,"stress2x":stress,"tradeLogFirst50":bt[:50],"authorization":"RESEARCH_ONLY","liveTrading":False,"generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),"notes":"Frozen candidate; 365d; 800-bar warmup; next-bar-open; actual-exit re-entry; canonical indicator and execution semantics for parity reconciliation."}
OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2)); print(json.dumps(out,indent=2))
