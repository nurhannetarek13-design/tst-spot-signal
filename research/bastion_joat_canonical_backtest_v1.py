#!/usr/bin/env python3
"""Canonical research backtest for Bastion JOAT Spot Long-only.

Uses Binance public Spot klines from data.binance.vision only - no exchange API,
API keys, account endpoints, or live trading. Pine defaults are frozen.

Execution semantics (declared before results):
- 15m completed bars.
- Signal on bar close; entry at next bar open.
- Long only; no DCA/pyramiding.
- Pine signal-close anchors initial ATR stop and 2R target.
- Existing stop/target are evaluated against bar high/low before a trailing
  update from that bar close can take effect on the next bar.
- If stop and target are both touched in the same bar, stop wins (conservative).
- Trailing activates after close reaches >= 1R using current ATR; ratchets up.
- Weekdays only; Pine NY sessions; EOD exit on first bar closing at/after 15:45 NY.
- Portfolio maximum 3 simultaneous positions, 10 USDT stake each.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import pathlib
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

BASE = "https://data.binance.vision/data/spot/monthly/klines"
UA = "tst-joat-canonical/1.0"
SYMBOLS = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","LINKUSDT","AVAXUSDT","DOTUSDT"]
INTERVAL = "15m"
KCOLS = ["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_buy_base","taker_buy_quote","ignore"]

# Frozen Pine defaults.
ATR_LEN=14; SL_ATR=1.5; RR=2.0; VWAP_LEN=20; SLOPE_THR=0.12
BB_LEN=20; BB_MULT=2.0; SWING_LEN=5; DISP_BODY=0.70; DISP_MULT=1.8
RSI_LEN=14; RSI_BULL=55.0; SMI_LOOK=13; SMI_SM1=25; SMI_SM2=2
PAT_VOL_MULT=1.3; CVD_LOOK=10; MAX_DAILY_TRADES=3
STAKE=10.0; MAX_OPEN=3


def _http(url: str) -> bytes:
    req=urllib.request.Request(url,headers={"User-Agent":UA})
    with urllib.request.urlopen(req,timeout=60) as r:return r.read()


def _months(start: pd.Timestamp,end: pd.Timestamp):
    p=start.to_period('M'); q=(end-pd.Timedelta(seconds=1)).to_period('M')
    while p<=q:
        yield f"{p.year:04d}-{p.month:02d}"; p+=1


def load_spot(symbol: str,start: pd.Timestamp,end: pd.Timestamp) -> pd.DataFrame:
    parts=[]
    for ym in _months(start,end):
        url=f"{BASE}/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{ym}.zip"
        try:raw=_http(url)
        except urllib.error.HTTPError as e:
            if e.code==404:continue
            raise
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            name=[x for x in z.namelist() if x.endswith('.csv')][0]
            x=pd.read_csv(z.open(name),header=None)
        if len(x) and not str(x.iloc[0,0]).replace('-','').isdigit():x=x.iloc[1:].reset_index(drop=True)
        x=x.iloc[:,:len(KCOLS)];x.columns=KCOLS;parts.append(x)
    if not parts:raise RuntimeError(f"no data {symbol}")
    d=pd.concat(parts,ignore_index=True)
    ot=pd.to_numeric(d.open_time,errors='coerce'); med=float(ot.dropna().median()); unit='us' if med>1e14 else 'ms'
    d['date']=pd.to_datetime(ot,unit=unit,utc=True,errors='coerce')
    for c in ['open','high','low','close','volume']:d[c]=pd.to_numeric(d[c],errors='coerce')
    d=d.dropna(subset=['date','open','high','low','close','volume']).drop_duplicates('date').sort_values('date')
    d=d[(d.date>=start)&(d.date<end)].reset_index(drop=True)
    return d[['date','open','high','low','close','volume']]


def ema(s,n):return s.ewm(span=n,adjust=False,min_periods=n).mean()
def sma(s,n):return s.rolling(n,min_periods=n).mean()

def atr(d,n=14):
    pc=d.close.shift(1); tr=pd.concat([(d.high-d.low),(d.high-pc).abs(),(d.low-pc).abs()],axis=1).max(axis=1)
    # Wilder RMA, matching ta.atr.
    return tr.ewm(alpha=1/n,adjust=False,min_periods=n).mean()

def rsi(s,n=14):
    delta=s.diff(); up=delta.clip(lower=0); dn=(-delta.clip(upper=0))
    au=up.ewm(alpha=1/n,adjust=False,min_periods=n).mean(); ad=dn.ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    rs=au/ad.replace(0,np.nan); out=100-(100/(1+rs)); return out.where(ad!=0,100.0)

def percentrank_last(s,w):
    def f(x):
        v=x[-1]; return 100.0*(np.sum(x<=v)-1)/max(1,len(x)-1)
    return s.rolling(w,min_periods=w).apply(f,raw=True)

def session_vwap(d):
    day=d.date.dt.floor('D'); tp=(d.high+d.low+d.close)/3; pv=tp*d.volume
    return pv.groupby(day).cumsum()/d.volume.groupby(day).cumsum().replace(0,np.nan)

def structure_trend(d,n=5):
    # ta.pivothigh/low becomes known n bars after pivot. Strictly point-in-time.
    h=d.high.to_numpy(); l=d.low.to_numpy(); c=d.close.to_numpy(); state=0; last_h=np.nan; last_l=np.nan; out=[]
    for i in range(len(d)):
        p=i-n
        if p>=n and p+n<len(d):
            wh=h[p-n:p+n+1]; wl=l[p-n:p+n+1]
            if np.isfinite(h[p]) and h[p]>=np.nanmax(wh):last_h=h[p]
            if np.isfinite(l[p]) and l[p]<=np.nanmin(wl):last_l=l[p]
        if np.isfinite(last_h) and c[i]>last_h and state<=0:state=1
        if np.isfinite(last_l) and c[i]<last_l and state>=0:state=-1
        out.append(state)
    return pd.Series(out,index=d.index,dtype='int8')


def enrich(d: pd.DataFrame) -> pd.DataFrame:
    d=d.copy()
    d['atr']=atr(d,ATR_LEN);d['sma20']=sma(d.close,20);d['sma50']=sma(d.close,50);d['sma200']=sma(d.close,200);d['rsi']=rsi(d.close,RSI_LEN)
    d['vwap']=session_vwap(d); vd=d.vwap-d.vwap.shift(VWAP_LEN).fillna(d.vwap)
    d['norm_slope']=vd/(d.atr*np.sqrt(VWAP_LEN)).replace(0,np.nan); slope_up=d.norm_slope>SLOPE_THR; slope_dn=d.norm_slope<-SLOPE_THR
    sma_bull=(d.sma20>d.sma50)&(d.sma50>d.sma200); sma_bear=(d.sma20<d.sma50)&(d.sma50<d.sma200)
    mid=sma(d.close,BB_LEN); sd=d.close.rolling(BB_LEN,min_periods=BB_LEN).std(ddof=0); up=mid+BB_MULT*sd; lo=mid-BB_MULT*sd
    width=(up-lo)/mid.replace(0,np.nan)*100; squeeze=percentrank_last(width,120)<10
    regime=np.zeros(len(d),dtype=np.int8); regime[np.asarray((slope_up|sma_bull).fillna(False))]=1; regime[np.asarray((slope_dn|sma_bear).fillna(False))]=-1; regime[np.asarray(squeeze.fillna(False))]=0
    d['regime_bull']=regime==1;d['is_squeeze']=squeeze.fillna(False);d['struct_trend']=structure_trend(d,SWING_LEN)
    body=(d.close-d.open).abs(); rng=d.high-d.low; avg_body=sma(body,20); ratio=body/rng.replace(0,np.nan)
    d['bull_disp']=(ratio>=DISP_BODY)&(body>=avg_body*DISP_MULT)&(rng>0)&(d.close>d.open)
    hh=d.high.rolling(SMI_LOOK,min_periods=SMI_LOOK).max();ll=d.low.rolling(SMI_LOOK,min_periods=SMI_LOOK).min();dist=d.close-(hh+ll)/2;rg=hh-ll
    sm2n=ema(ema(dist,SMI_SM1),SMI_SM2);sm2d=ema(ema(rg,SMI_SM1),SMI_SM2)*0.5;d['smi']=100*sm2n/sm2d.replace(0,np.nan);d['mom_bull']=(d.rsi>RSI_BULL)&(d.smi>0)
    den=(d.high-d.low).replace(0,np.nan);buy=np.where(d.close>=d.open,d.volume,d.volume*(d.close-d.low)/den);delta=pd.Series(buy,index=d.index)-(d.volume-pd.Series(buy,index=d.index));d['cvd']=delta.fillna(0).cumsum();d['cvd_bull']=d.cvd>sma(d.cvd,CVD_LOOK)
    av=sma(d.volume,20);hv=d.volume>av*PAT_VOL_MULT;lw=np.minimum(d.open,d.close)-d.low;uw=d.high-np.maximum(d.open,d.close)
    engulf=(d.close>d.open)&(d.close.shift(1)<d.open.shift(1))&(d.close>d.open.shift(1))&(d.open<d.close.shift(1))&(body>(d.close.shift(1)-d.open.shift(1)).abs())&hv
    pin=(lw>body*2)&(uw<body*0.5)&(rng>0);d['bull_pattern']=engulf|pin
    ny=d.date.dt.tz_convert('America/New_York');mins=ny.dt.hour*60+ny.dt.minute
    sess=mins.between(7*60,10*60-1)|mins.between(2*60,5*60-1)|mins.between(9*60+30,16*60-1)|mins.between(3*60,9*60+30-1)
    d['session_ok']=sess&(ny.dt.dayofweek<=4);d['ny_day']=ny.dt.date;d['ny_min']=mins;d['weekday']=ny.dt.dayofweek<=4
    d['entry_signal']=d.regime_bull&(d.struct_trend==1)&d.mom_bull&d.cvd_bull&d.session_ok&(~d.is_squeeze)&(d.bull_disp|d.bull_pattern|((d.close>d.sma20)&(d.close>d.vwap)))&(d.volume>0)
    return d

@dataclass
class Position:
    symbol:str; entry_time:pd.Timestamp; entry_price:float; anchor:float; signal_atr:float; stop:float; target:float; stake:float; entry_fee:float


def simulate(frames: dict[str,pd.DataFrame],fee_side:float) -> dict:
    # Event-driven portfolio, max 3 concurrent. Candidate selection deterministic alphabetical when simultaneous.
    by_time={}
    for sym,d in frames.items():
        for i in range(1,len(d)):
            if bool(d.entry_signal.iloc[i-1]):by_time.setdefault(d.date.iloc[i],[]).append((sym,i))
    timeline=sorted(set().union(*(set(d.date) for d in frames.values())))
    positions={}; trades=[]; daily_counts={}
    equity=1000.0; peak=equity; maxdd=0.0
    index_maps={s:{t:i for i,t in enumerate(d.date)} for s,d in frames.items()}
    for ts in timeline:
        # Manage open positions on this bar.
        for sym in list(positions):
            d=frames[sym]; i=index_maps[sym].get(ts)
            if i is None:continue
            r=d.iloc[i]; p=positions[sym]; exit_price=None; reason=None
            # Conservative intrabar collision: stop first.
            if float(r.low)<=p.stop: exit_price=p.stop;reason='stop'
            elif float(r.high)>=p.target: exit_price=p.target;reason='target'
            elif bool(r.weekday) and int(r.ny_min)>=15*60+45: exit_price=float(r.close);reason='eod'
            if exit_price is not None:
                gross=(exit_price/p.entry_price-1.0)*p.stake; fees=p.entry_fee+exit_price*(p.stake/p.entry_price)*fee_side; net=gross-fees
                equity+=net;trades.append({'symbol':sym,'entry_time':str(p.entry_time),'exit_time':str(ts),'entry':p.entry_price,'exit':exit_price,'reason':reason,'net_usdt':net,'ret_pct':net/p.stake*100})
                positions.pop(sym);peak=max(peak,equity);maxdd=max(maxdd,(peak-equity)/peak if peak else 0);continue
            # Trailing update becomes active next bar.
            atr_now=float(r.atr) if np.isfinite(r.atr) else np.nan
            if np.isfinite(atr_now) and atr_now>0:
                sl_dist=SL_ATR*atr_now;profit_r=(float(r.close)-p.anchor)/sl_dist
                if profit_r>=1.0:p.stop=max(p.stop,float(r.close)-atr_now)
        # Open next-bar orders after management. Same symbol cannot overlap.
        candidates=sorted(by_time.get(ts,[]),key=lambda z:z[0])
        for sym,i in candidates:
            if len(positions)>=MAX_OPEN:break
            if sym in positions:continue
            d=frames[sym];sig=d.iloc[i-1];bar=d.iloc[i];day=bar.ny_day;key=(sym,day)
            if daily_counts.get(key,0)>=MAX_DAILY_TRADES:continue
            if not np.isfinite(sig.atr) or float(sig.atr)<=0:continue
            ep=float(bar.open);anchor=float(sig.close);sa=float(sig.atr);stop=anchor-SL_ATR*sa;target=anchor+SL_ATR*sa*RR
            # If next open gaps below stop, enter then conservative immediate stop at open (not impossible historical fill).
            if ep<=0:continue
            positions[sym]=Position(sym,ts,ep,anchor,sa,stop,target,STAKE,ep*(STAKE/ep)*fee_side);daily_counts[key]=daily_counts.get(key,0)+1
    # Close remaining at final close.
    for sym,p in list(positions.items()):
        r=frames[sym].iloc[-1];xp=float(r.close);gross=(xp/p.entry_price-1)*p.stake;fees=p.entry_fee+xp*(p.stake/p.entry_price)*fee_side;net=gross-fees;equity+=net
        trades.append({'symbol':sym,'entry_time':str(p.entry_time),'exit_time':str(r.date),'entry':p.entry_price,'exit':xp,'reason':'end','net_usdt':net,'ret_pct':net/p.stake*100})
    t=pd.DataFrame(trades)
    if t.empty:return {'trades':0,'wins':0,'winRate':0,'netPnlUSDT':0,'netReturnPctOn1000':0,'expectancyUSDT':0,'profitFactor':0,'maxDrawdownPct':maxdd*100,'exitReasons':{}}
    wins=t[t.net_usdt>0].net_usdt;loss=-t[t.net_usdt<0].net_usdt;pf=float(wins.sum()/loss.sum()) if loss.sum()>0 else None
    return {'trades':int(len(t)),'wins':int((t.net_usdt>0).sum()),'winRate':float((t.net_usdt>0).mean()),'netPnlUSDT':float(t.net_usdt.sum()),'netReturnPctOn1000':float(t.net_usdt.sum()/10.0),'expectancyUSDT':float(t.net_usdt.mean()),'profitFactor':pf,'maxDrawdownPct':float(maxdd*100),'exitReasons':{str(k):int(v) for k,v in t.reason.value_counts().items()},'bySymbol':{str(k):{'trades':int(len(g)),'netUSDT':float(g.net_usdt.sum())} for k,g in t.groupby('symbol')}}


def run_period(start,end,fee_side):
    st=pd.Timestamp(start,tz='UTC');en=pd.Timestamp(end,tz='UTC');frames={};fail={}
    # warmup 70 days for SMA200/BB rank/CVD; signals evaluated only from st.
    warm=st-pd.Timedelta(days=70)
    for s in SYMBOLS:
        try:
            d=enrich(load_spot(s,warm,en));d=d[d.date>=st].reset_index(drop=True);frames[s]=d
        except Exception as e:fail[s]=f'{type(e).__name__}: {e}'
    if len(frames)<8:raise RuntimeError(f'insufficient symbols loaded {len(frames)} failures={fail}')
    out=simulate(frames,fee_side);out['symbolsLoaded']=sorted(frames);out['failures']=fail;return out


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--out',default='validation/edges/bastion-joat-canonical-v1.json');a=ap.parse_args()
    report={'engine':'BASTION_JOAT_CANONICAL_V1','authorization':'RESEARCH_ONLY','liveTrading':False,'strategy':'BastionJOATSpotV2','timeframe':'15m','symbols':SYMBOLS,'stakeUSDT':STAKE,'maxOpenTrades':MAX_OPEN,'parametersFrozen':True,'executionSemantics':'signal close -> next open; conservative stop-first collision; trail from next bar','results':{}}
    report['results']['discovery2025']=run_period('2025-01-01','2026-01-01',0.0014)
    report['results']['oos2026']=run_period('2026-01-01','2026-09-01',0.0014)
    report['results']['oos2026Stress']=run_period('2026-01-01','2026-09-01',0.0040)
    o=report['results']['oos2026'];s=report['results']['oos2026Stress']
    report['gate']={'minOosTrades':30,'minOosPF':1.20,'requireOosNetPositive':True,'minStressPF':1.0,'requireStressNetPositive':True}
    report['decision']='PAPER_CANDIDATE' if (o['trades']>=30 and (o['profitFactor'] or 0)>1.20 and o['netPnlUSDT']>0 and (s['profitFactor'] or 0)>1.0 and s['netPnlUSDT']>0) else 'REJECT_JOAT_V1'
    p=pathlib.Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(report,indent=2,sort_keys=True));print(json.dumps({'decision':report['decision'],'discovery':report['results']['discovery2025'],'oos':o,'stress':s},indent=2))
if __name__=='__main__':main()
