import json
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd
from pandas import DataFrame
from freqtrade.strategy import IStrategy

CANDIDATE_PATHS=[Path("/freqtrade/user_data/candidate-manifest.json"),Path("freqtrade/user_data/candidate-manifest.json"),Path("validation/fusion/frozen-parity-candidate.json")]
_manifest={}
for _p in CANDIDATE_PATHS:
    try:
        if _p.exists(): _manifest=json.loads(_p.read_text()); break
    except Exception: pass
FAMILY=_manifest.get("family","TS_MOMENTUM"); PARAMS=_manifest.get("params") or {}; TIMEFRAME=_manifest.get("timeframe","1h")

def ema(s,n): return s.ewm(span=int(n),adjust=False).mean()
def rsi_wilder(s,n=14):
    d=s.diff(); g=d.clip(lower=0); l=-d.clip(upper=0); ag=g.ewm(alpha=1/n,adjust=False).mean(); al=l.ewm(alpha=1/n,adjust=False).mean(); rs=ag/al.replace(0,np.nan); return (100-100/(1+rs)).fillna(50.0)
def atr_pct_sma(df,n=14):
    pc=df.close.shift(1); tr=pd.concat([(df.high-df.low).abs(),(df.high-pc).abs(),(df.low-pc).abs()],axis=1).max(axis=1); return tr.rolling(n).mean()/df.close

class UnifiedCandidateStrategy(IStrategy):
    INTERFACE_VERSION=3; timeframe=TIMEFRAME; can_short=False; process_only_new_candles=True; startup_candle_count=800
    stoploss=-float(PARAMS.get("sl",0.03)); minimal_roi={"0":100.0}; use_custom_roi=True; trailing_stop=False; use_exit_signal=True; exit_profit_only=False; ignore_roi_if_entry_signal=False
    # Canonical simulator evaluates a signal only after the previous position is
    # fully closed, so the candle containing an exit cannot seed the next entry.
    # Two Freqtrade cooldown candles are required because protections count the
    # close candle as the first candle; this suppresses the immediately-following
    # entry candle and restores next-bar-open parity.
    @property
    def protections(self): return [{"method":"CooldownPeriod","stop_duration_candles":2}]
    def custom_roi(self,pair,trade,current_time,trade_duration,entry_tag,side,**kwargs):
        tp=float(PARAMS.get("tp",0.06)); fo=float(getattr(trade,"fee_open",0.0) or 0.0); fc=float(getattr(trade,"fee_close",fo) or fo); return (1+tp)*(1-fc)/(1+fo)-1
    def informative_pairs(self): return [("BTC/USDT","1h"),("ETH/USDT","1h"),("SOL/USDT","1h")] if FAMILY=="CROSS_CRYPTO_LEAD_LAG" else []
    def populate_indicators(self,df:DataFrame,metadata:dict)->DataFrame:
        p=PARAMS; df["rsi"]=rsi_wilder(df.close,14); df["atr_pct"]=atr_pct_sma(df,14); df["qv"]=df.volume*df.close; df["qv_med24"]=df.qv.rolling(24).median(); df["relvol"]=df.qv/df.qv_med24.replace(0,np.nan)
        if FAMILY=="TS_MOMENTUM":
            df["ema_fast"]=ema(df.close,int(p.get("emaFast",48))); df["ema_slow"]=ema(df.close,int(p.get("emaSlow",120))); df["ret_lb"]=df.close.pct_change(int(p.get("retLookback",24)))
        elif FAMILY=="MEAN_REVERSION":
            df["mid"]=df.close.rolling(20).mean(); df["sd"]=df.close.rolling(20).std(ddof=0); df["lower"]=df.mid-float(p.get("bb",2))*df.sd
        elif FAMILY=="TREND_BREAKOUT":
            df["ema_fast"]=ema(df.close,int(p.get("fast",20))); df["ema_slow"]=ema(df.close,int(p.get("slow",60))); df["hh"]=df.high.rolling(int(p.get("lookback",20))).max().shift(1)
        elif FAMILY=="VOLATILITY_MOMENTUM": df["hh"]=df.high.rolling(int(p.get("lookback",20))).max().shift(1); df["ema20"]=ema(df.close,20)
        elif FAMILY=="VOLATILITY_BREAKOUT":
            df["hh"]=df.high.rolling(int(p.get("lookback",24))).max().shift(1); df["atr_rank"]=df.atr_pct.rolling(int(p.get("compressionLookback",72))).rank(pct=True)
        elif FAMILY=="LIQUIDITY_REVERSAL":
            lb=int(p.get("retLookback",6)); zlb=int(p.get("zLookback",720)); df["ret_lb"]=df.close.pct_change(lb); df["ret_mu"]=df.ret_lb.rolling(zlb).mean(); df["ret_sd"]=df.ret_lb.rolling(zlb).std(ddof=0); df["z"]=(df.ret_lb-df.ret_mu)/df.ret_sd.replace(0,np.nan); df["qv_week_med"]=df.qv.rolling(168).median(); df["volume_ratio"]=df.qv/df.qv_week_med.replace(0,np.nan); df["ema24"]=ema(df.close,24)
        return df
    def populate_entry_trend(self,df:DataFrame,metadata:dict)->DataFrame:
        p=PARAMS; df["enter_long"]=0; df["enter_tag"]=None
        if FAMILY=="TS_MOMENTUM": c=(df.close>df.ema_fast)&(df.ema_fast>df.ema_slow)&(df.ret_lb>float(p.get("retMin",.02)))&df.atr_pct.between(float(p.get("atrMin",.006)),float(p.get("atrMax",.08)))&(df.relvol>=float(p.get("relvol",.8)))&(df.volume>0)
        elif FAMILY=="MEAN_REVERSION": c=(df.close<df.lower)&(df.rsi<=float(p.get("rsi_in",34)))&(df.relvol>=.75)&(df.volume>0)
        elif FAMILY=="TREND_BREAKOUT": c=(df.ema_fast>df.ema_slow)&(df.close>df.hh)&(df.relvol>=float(p.get("relvol",1)))&df.rsi.between(52,72)&(df.volume>0)
        elif FAMILY=="VOLATILITY_MOMENTUM": c=(df.close>df.hh)&(df.relvol>=float(p.get("relvol",1.4)))&df.rsi.between(float(p.get("rsi_min",52)),74)&(df.close>df.ema20)&(df.volume>0)
        elif FAMILY=="VOLATILITY_BREAKOUT": c=(df.atr_rank.shift(1)<=float(p.get("compressionPct",.25)))&(df.close>df.hh)&(df.relvol>=float(p.get("relvol",1.5)))&df.rsi.between(float(p.get("rsiMin",55)),float(p.get("rsiMax",75)))&(df.volume>0)
        elif FAMILY=="LIQUIDITY_REVERSAL": c=(df.z<=float(p.get("zMax",-2)))&(df.volume_ratio<=float(p.get("volumeRatioMax",1.1)))&(df.rsi<=float(p.get("rsiMax",35)))&(df.close<df.ema24)&(df.volume>0)
        else: c=df.volume<0
        df.loc[c,["enter_long","enter_tag"]]=(1,FAMILY.lower()); return df
    def populate_exit_trend(self,df:DataFrame,metadata:dict)->DataFrame:
        df["exit_long"]=0; df["exit_tag"]=None; return df
    def custom_exit(self,pair,trade,current_time:datetime,current_rate,current_profit,**kwargs):
        hold=int(PARAMS.get("holdBars",0)); minutes=60 if TIMEFRAME=="1h" else 15
        return "time_exit" if hold>0 and (current_time-trade.open_date_utc).total_seconds()/60>=hold*minutes else None
