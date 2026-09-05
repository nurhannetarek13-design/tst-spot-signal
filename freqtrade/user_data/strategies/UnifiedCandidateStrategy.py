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

def ema(series,n): return series.ewm(span=int(n),adjust=False).mean()
def rsi_wilder(series,n=14):
    d=series.diff(); gain=d.clip(lower=0); loss=-d.clip(upper=0)
    ag=gain.ewm(alpha=1/n,adjust=False).mean(); al=loss.ewm(alpha=1/n,adjust=False).mean(); rs=ag/al.replace(0,np.nan)
    return (100-100/(1+rs)).fillna(50.0)
def atr_pct_sma(df,n=14):
    pc=df["close"].shift(1); tr=pd.concat([(df["high"]-df["low"]).abs(),(df["high"]-pc).abs(),(df["low"]-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()/df["close"]

class UnifiedCandidateStrategy(IStrategy):
    INTERFACE_VERSION=3; timeframe=TIMEFRAME; can_short=False; process_only_new_candles=True; startup_candle_count=800
    stoploss=-float(PARAMS.get("sl",0.03)); minimal_roi={"0":float(PARAMS.get("tp",0.06))}; trailing_stop=False
    use_exit_signal=True; exit_profit_only=False; ignore_roi_if_entry_signal=False

    @property
    def protections(self):
        # Canonical simulator cannot consume an entry signal from the candle in which
        # the previous position was still open. Freqtrade otherwise closes first and
        # can immediately consume that same candle's signal for the next-bar entry.
        return [{"method":"CooldownPeriod","stop_duration_candles":1}]

    def informative_pairs(self):
        return [("BTC/USDT","1h"),("ETH/USDT","1h"),("SOL/USDT","1h")] if FAMILY=="CROSS_CRYPTO_LEAD_LAG" else []

    def populate_indicators(self,df:DataFrame,metadata:dict)->DataFrame:
        p=PARAMS; df["rsi"]=rsi_wilder(df["close"],14); df["atr_pct"]=atr_pct_sma(df,14); df["qv"]=df["volume"]*df["close"]; df["qv_med24"]=df["qv"].rolling(24).median(); df["relvol"]=df["qv"]/df["qv_med24"].replace(0,np.nan)
        if FAMILY=="CROSS_CRYPTO_LEAD_LAG":
            leaders=[]
            if self.dp:
                for pair in ["BTC/USDT","ETH/USDT","SOL/USDT"]:
                    inf=self.dp.get_pair_dataframe(pair=pair,timeframe="1h").copy(); inf["leader_ret3"]=inf["close"].pct_change(3); leaders.append(inf[["date","leader_ret3"]].rename(columns={"leader_ret3":f"lead_{pair.split('/')[0]}"}))
                for z in leaders: df=df.merge(z,on="date",how="left")
                cols=[x for x in ["lead_BTC","lead_ETH","lead_SOL"] if x in df.columns]; df["leader3"]=df[cols].mean(axis=1) if cols else 0.0
            else: df["leader3"]=0.0
            df["ret3"]=df["close"].pct_change(3); df["gap"]=df["leader3"]-df["ret3"]; df["ema_fast"]=ema(df["close"],int(p.get("emaFast",24)))
        elif FAMILY=="TS_MOMENTUM":
            f=int(p.get("emaFast",48)); s=int(p.get("emaSlow",120)); lb=int(p.get("retLookback",24)); df["ema_fast"]=ema(df["close"],f); df["ema_slow"]=ema(df["close"],s); df["ret_lb"]=df["close"].pct_change(lb)
        elif FAMILY=="LIQUIDITY_REVERSAL":
            lb=int(p.get("retLookback",6)); zlb=int(p.get("zLookback",720)); df["ret_lb"]=df["close"].pct_change(lb); df["ret_mu"]=df["ret_lb"].rolling(zlb).mean(); df["ret_sd"]=df["ret_lb"].rolling(zlb).std(ddof=0); df["z"]=(df["ret_lb"]-df["ret_mu"])/df["ret_sd"].replace(0,np.nan); df["qv_week_med"]=df["qv"].rolling(7*24).median(); df["volume_ratio"]=df["qv"]/df["qv_week_med"].replace(0,np.nan); df["ema24"]=ema(df["close"],24)
        elif FAMILY=="VOLATILITY_BREAKOUT":
            lb=int(p.get("lookback",24)); clb=int(p.get("compressionLookback",72)); df["hh"]=df["high"].rolling(lb).max().shift(1); df["atr_rank"]=df["atr_pct"].rolling(clb).rank(pct=True)
        elif FAMILY=="TREND_BREAKOUT":
            df["ema_fast"]=ema(df["close"],int(p.get("fast",20))); df["ema_slow"]=ema(df["close"],int(p.get("slow",60))); df["hh"]=df["high"].rolling(int(p.get("lookback",20))).max().shift(1)
        elif FAMILY=="MEAN_REVERSION":
            df["mid"]=df["close"].rolling(20).mean(); df["sd"]=df["close"].rolling(20).std(ddof=0); df["lower"]=df["mid"]-float(p.get("bb",2.0))*df["sd"]
        elif FAMILY=="VOLATILITY_MOMENTUM": df["hh"]=df["high"].rolling(int(p.get("lookback",20))).max().shift(1); df["ema20"]=ema(df["close"],20)
        return df

    def populate_entry_trend(self,df:DataFrame,metadata:dict)->DataFrame:
        p=PARAMS; df["enter_long"]=0; df["enter_tag"]=None
        if FAMILY=="CROSS_CRYPTO_LEAD_LAG": cond=(df["leader3"]>=float(p.get("leaderRetMin",0.012)))&(df["gap"]>=float(p.get("gapMin",0.008)))&(df["ret3"]>float(p.get("altRetMin",-0.02)))&(df["close"]>df["ema_fast"])&(df["relvol"]>=float(p.get("relvol",0.9)))&df["rsi"].between(float(p.get("rsiMin",42)),float(p.get("rsiMax",70)))&(df["volume"]>0)
        elif FAMILY=="TS_MOMENTUM": cond=(df["close"]>df["ema_fast"])&(df["ema_fast"]>df["ema_slow"])&(df["ret_lb"]>float(p.get("retMin",0.02)))&df["atr_pct"].between(float(p.get("atrMin",0.006)),float(p.get("atrMax",0.08)))&(df["relvol"]>=float(p.get("relvol",0.8)))&(df["volume"]>0)
        elif FAMILY=="LIQUIDITY_REVERSAL": cond=(df["z"]<=float(p.get("zMax",-2.0)))&(df["volume_ratio"]<=float(p.get("volumeRatioMax",1.10)))&(df["rsi"]<=float(p.get("rsiMax",35)))&(df["close"]<df["ema24"])&(df["volume"]>0)
        elif FAMILY=="VOLATILITY_BREAKOUT": cond=(df["atr_rank"].shift(1)<=float(p.get("compressionPct",0.25)))&(df["close"]>df["hh"])&(df["relvol"]>=float(p.get("relvol",1.5)))&df["rsi"].between(float(p.get("rsiMin",55)),float(p.get("rsiMax",75)))&(df["volume"]>0)
        elif FAMILY=="TREND_BREAKOUT": cond=(df["ema_fast"]>df["ema_slow"])&(df["close"]>df["hh"])&(df["relvol"]>=float(p.get("relvol",1.0)))&df["rsi"].between(52,72)&(df["volume"]>0)
        elif FAMILY=="MEAN_REVERSION": cond=(df["close"]<df["lower"])&(df["rsi"]<=float(p.get("rsi_in",34)))&(df["relvol"]>=0.75)&(df["volume"]>0)
        elif FAMILY=="VOLATILITY_MOMENTUM": cond=(df["close"]>df["hh"])&(df["relvol"]>=float(p.get("relvol",1.4)))&df["rsi"].between(float(p.get("rsi_min",52)),74)&(df["close"]>df["ema20"])&(df["volume"]>0)
        else: cond=(df["volume"]<0)
        df.loc[cond,["enter_long","enter_tag"]]=(1,FAMILY.lower()); return df

    def populate_exit_trend(self,df:DataFrame,metadata:dict)->DataFrame:
        p=PARAMS; df["exit_long"]=0; df["exit_tag"]=None
        if FAMILY=="MEAN_REVERSION": cond=(df["close"]>=df["mid"])|(df["rsi"]>=float(p.get("rsi_out",50))); df.loc[cond,["exit_long","exit_tag"]]=(1,"mean_reversion_done")
        elif FAMILY=="TREND_BREAKOUT": cond=(df["ema_fast"]<df["ema_slow"])|(df["rsi"]<45); df.loc[cond,["exit_long","exit_tag"]]=(1,"trend_failure")
        elif FAMILY=="VOLATILITY_MOMENTUM": cond=(df["close"]<df["ema20"])|(df["rsi"]<45); df.loc[cond,["exit_long","exit_tag"]]=(1,"vol_failure")
        return df

    def custom_exit(self,pair,trade,current_time:datetime,current_rate,current_profit,**kwargs):
        hold=int(PARAMS.get("holdBars",0)); minutes=60 if TIMEFRAME=="1h" else 15
        if hold>0 and (current_time-trade.open_date_utc).total_seconds()/60>=hold*minutes: return "time_exit"
        return None
