#!/usr/bin/env python3
"""Frozen TradingView-inspired candidate discovery for Binance Spot 15m.

Research only. Long-only. Signal at completed-bar close, fill at next bar open.
All candidates share the SAME execution model so we compare entry logic rather
than TradingView fill semantics: 2 ATR initial stop, 2R target, 48-bar time exit.
Stop wins same-bar stop/target collisions. Fees + slippage applied both sides.
No parameter optimization is performed.
"""
from __future__ import annotations
import argparse, io, json, math, pathlib, urllib.error, urllib.request, zipfile
import numpy as np
import pandas as pd

BASE="https://data.binance.vision/data/spot/monthly/klines"
INTERVAL="15m"
KCOLS=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_buy_base","taker_buy_quote","ignore"]
UA="tst-tv-candidate-discovery/1.0"


def http(url):
    req=urllib.request.Request(url,headers={"User-Agent":UA})
    with urllib.request.urlopen(req,timeout=60) as r:return r.read()

def months(start,end):
    p=start.to_period("M"); q=(end-pd.Timedelta(seconds=1)).to_period("M")
    while p<=q:
        yield f"{p.year:04d}-{p.month:02d}"; p+=1

def load(symbol,start,end):
    parts=[]
    for ym in months(start,end):
        url=f"{BASE}/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{ym}.zip"
        try: raw=http(url)
        except urllib.error.HTTPError as e:
            if e.code==404: continue
            raise
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            name=next(n for n in z.namelist() if n.endswith('.csv'))
            x=pd.read_csv(z.open(name),header=None)
        if len(x) and not str(x.iloc[0,0]).replace('-','').isdigit(): x=x.iloc[1:].reset_index(drop=True)
        x=x.iloc[:,:len(KCOLS)]; x.columns=KCOLS; parts.append(x)
    if not parts: raise RuntimeError(f"no data for {symbol}")
    d=pd.concat(parts,ignore_index=True)
    ot=pd.to_numeric(d.open_time,errors="coerce"); unit="us" if float(ot.dropna().median())>1e14 else "ms"
    d["date"]=pd.to_datetime(ot,unit=unit,utc=True,errors="coerce")
    for c in ["open","high","low","close","volume"]: d[c]=pd.to_numeric(d[c],errors="coerce")
    d=d.dropna(subset=["date","open","high","low","close","volume"]).drop_duplicates("date").sort_values("date")
    return d[(d.date>=start)&(d.date<end)][["date","open","high","low","close","volume"]].reset_index(drop=True)

def ema(s,n): return s.ewm(span=n,adjust=False,min_periods=n).mean()
def sma(s,n): return s.rolling(n,min_periods=n).mean()
def atr(d,n=14):
    pc=d.close.shift(1)
    tr=pd.concat([(d.high-d.low),(d.high-pc).abs(),(d.low-pc).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n,adjust=False,min_periods=n).mean()
def rsi(s,n=14):
    delta=s.diff(); up=delta.clip(lower=0); dn=(-delta.clip(upper=0))
    au=up.ewm(alpha=1/n,adjust=False,min_periods=n).mean(); ad=dn.ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    rs=au/ad.replace(0,np.nan); out=100-(100/(1+rs)); return out.where(ad!=0,100.0)
def adx(d,n=14):
    up=d.high.diff(); down=-d.low.diff()
    plus=pd.Series(np.where((up>down)&(up>0),up,0.0),index=d.index)
    minus=pd.Series(np.where((down>up)&(down>0),down,0.0),index=d.index)
    pc=d.close.shift(1)
    tr=pd.concat([(d.high-d.low),(d.high-pc).abs(),(d.low-pc).abs()],axis=1).max(axis=1)
    trr=tr.ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    pdi=100*plus.ewm(alpha=1/n,adjust=False,min_periods=n).mean()/trr.replace(0,np.nan)
    mdi=100*minus.ewm(alpha=1/n,adjust=False,min_periods=n).mean()/trr.replace(0,np.nan)
    dx=100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
    return dx.ewm(alpha=1/n,adjust=False,min_periods=n).mean()

def enrich(d):
    d=d.copy(); d["atr14"]=atr(d,14); d["ema200"]=ema(d.close,200); d["ema220"]=ema(d.close,220)
    d["vol30"]=sma(d.volume,30); d["vol18"]=sma(d.volume,18); d["rsi14"]=rsi(d.close,14); d["adx14"]=adx(d,14)
    d["don20"]=d.high.shift(1).rolling(20,min_periods=20).max()
    # MACD 12/26 with EMA signal 9 (fixed, not optimized)
    d["macd"]=ema(d.close,12)-ema(d.close,26); d["macdsig"]=ema(d.macd,9)
    d["macd_cross_up"]=(d.macd>d.macdsig)&(d.macd.shift(1)<=d.macdsig.shift(1))
    # Keltner from supplied Volatility Breakout script defaults.
    kc_basis=ema(d.close,22); kc_range=atr(d,10)*2.0; d["kc_upper"]=kc_basis+kc_range
    d["kc_cross_up"]=(d.close>d.kc_upper)&(d.close.shift(1)<=d.kc_upper.shift(1))
    return d

def signals(d):
    base=(d.close>d.don20)
    return {
      "DONCHIAN20": base,
      "DONCHIAN20_EMA200": base&(d.close>d.ema200),
      "DONCHIAN20_EMA200_VOL": base&(d.close>d.ema200)&(d.volume>d.vol30*1.2),
      "MACD_EMA200": d.macd_cross_up&(d.macd<0)&(d.close>d.ema200),
      "MACD_EMA200_ADX": d.macd_cross_up&(d.macd<0)&(d.close>d.ema200)&(d.adx14>25),
      "VOLATILITY_BREAKOUT": d.kc_cross_up&(d.volume>d.vol18)&(d.close>d.ema220)&(d.rsi14>50)&(d.adx14>20),
    }

def metrics(trades, initial=1000.0):
    if not trades: return {"trades":0,"winRate":0.0,"profitFactor":0.0,"expectancyUSDT":0.0,"netUSDT":0.0,"maxDrawdownPct":0.0}
    t=pd.DataFrame(trades); wins=t.loc[t.net>0,"net"].sum(); losses=-t.loc[t.net<0,"net"].sum()
    pf=float(wins/losses) if losses>0 else None
    eq=initial+t.net.cumsum(); peak=eq.cummax(); dd=(peak-eq)/peak.replace(0,np.nan)
    return {"trades":int(len(t)),"winRate":float((t.net>0).mean()),"profitFactor":pf,"expectancyUSDT":float(t.net.mean()),"netUSDT":float(t.net.sum()),"maxDrawdownPct":float(dd.max()*100 if len(dd) else 0)}

def simulate(d, sig, fee, slip_bps, stake=100.0):
    slip=slip_bps/10000.0; trades=[]; i=1
    while i<len(d):
        if not bool(sig.iloc[i-1]): i+=1; continue
        s=d.iloc[i-1]; b=d.iloc[i]
        if not np.isfinite(s.atr14) or s.atr14<=0: i+=1; continue
        entry=float(b.open)*(1+slip); risk=2.0*float(s.atr14); stop=float(s.close)-risk; target=float(s.close)+2.0*risk
        if entry<=0 or stop<=0: i+=1; continue
        exitp=None; reason=None; j=i
        maxj=min(len(d)-1,i+48)
        while j<=maxj:
            r=d.iloc[j]
            if float(r.low)<=stop: exitp=stop*(1-slip); reason="stop"; break
            if float(r.high)>=target: exitp=target*(1-slip); reason="target"; break
            if j==maxj: exitp=float(r.close)*(1-slip); reason="time"; break
            j+=1
        qty=stake/entry; gross=(exitp-entry)*qty; fees=(entry*qty+exitp*qty)*fee; net=gross-fees
        trades.append({"entry":entry,"exit":exitp,"net":net,"reason":reason,"entry_time":str(b.date),"exit_time":str(d.iloc[j].date)})
        i=j+1
    return trades

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--start",required=True); ap.add_argument("--end",required=True); ap.add_argument("--symbols",nargs="+",required=True)
    ap.add_argument("--fee-side",type=float,default=0.001); ap.add_argument("--slippage-bps",type=float,default=2.0); ap.add_argument("--out",required=True); a=ap.parse_args()
    start=pd.Timestamp(a.start,tz="UTC"); end=pd.Timestamp(a.end,tz="UTC")
    frames={s:enrich(load(s,start,end)) for s in a.symbols}
    # Strict chronological 70/30 split per symbol.
    splits={s:int(len(d)*0.70) for s,d in frames.items()}
    names=list(signals(next(iter(frames.values()))).keys()); result={}
    for name in names:
        rec={"IS":{},"OOS":{}}
        for phase in ["IS","OOS"]:
            alltr=[]; by={}
            for s,d in frames.items():
                k=splits[s]; part=d.iloc[:k].copy().reset_index(drop=True) if phase=="IS" else d.iloc[k:].copy().reset_index(drop=True)
                sg=signals(part)[name].fillna(False); tr=simulate(part,sg,a.fee_side,a.slippage_bps); by[s]=metrics(tr); alltr.extend([{**x,"symbol":s} for x in tr])
            agg=metrics(alltr); agg["bySymbol"]=by; rec[phase]=agg
        o=rec["OOS"]; isp=rec["IS"]
        positives=sum(1 for s,m in o["bySymbol"].items() if m["expectancyUSDT"]>0)
        mintr=min((m["trades"] for m in o["bySymbol"].values()),default=0)
        rec["gate"]={
          "aggregatePfGte1_3": bool(o["profitFactor"] is not None and o["profitFactor"]>=1.3),
          "oosTradesGte60": o["trades"]>=60,
          "maxDdLte15Pct": o["maxDrawdownPct"]<=15,
          "positiveExpectancyAtLeast2of3": positives>=2,
          "min10TradesEachSymbol": mintr>=10,
          "isPfGte1_1": bool(isp["profitFactor"] is not None and isp["profitFactor"]>=1.1),
        }
        rec["gate"]["passAll"]=all(rec["gate"].values())
        result[name]=rec
    ranking=sorted(result,key=lambda n: ((result[n]["OOS"]["profitFactor"] or -1),result[n]["OOS"]["expectancyUSDT"]),reverse=True)
    out={"authorization":"RESEARCH_ONLY","liveTrading":False,"decision":"DIAGNOSTIC_ONLY_NOT_EDGE_APPROVAL","timeframe":"15m","longOnly":True,
         "execution":{"signal":"bar close","entry":"next bar open","stop":"2x ATR14","target":"2R","maxHoldBars":48,"feesPerSide":a.fee_side,"slippageBpsPerSide":a.slippage_bps,"collision":"stop_wins"},
         "period":{"start":a.start,"end":a.end,"split":"70pct_IS_30pct_OOS"},"symbols":a.symbols,"ranking":ranking,"candidates":result}
    p=pathlib.Path(a.out); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(out,indent=2),encoding="utf-8"); print(json.dumps(out,indent=2))
if __name__=="__main__": main()
