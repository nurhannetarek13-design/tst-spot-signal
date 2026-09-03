import json
from datetime import datetime
from pathlib import Path
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy

CANDIDATE_PATHS=[
    Path("/freqtrade/user_data/candidate-manifest.json"),
    Path("freqtrade/user_data/candidate-manifest.json"),
    Path("validation/fusion/candidate-manifest.json"),
]
_manifest={}
for _p in CANDIDATE_PATHS:
    try:
        if _p.exists():
            _manifest=json.loads(_p.read_text())
            break
    except Exception:
        pass

FAMILY=_manifest.get("family","TS_MOMENTUM")
PARAMS=_manifest.get("params") or {}
TIMEFRAME=_manifest.get("timeframe","1h")


class UnifiedCandidateStrategy(IStrategy):
    INTERFACE_VERSION=3
    timeframe=TIMEFRAME
    can_short=False
    process_only_new_candles=True
    startup_candle_count=800

    stoploss=-float(PARAMS.get("sl",0.03))
    minimal_roi={"0": float(PARAMS.get("tp",0.06))}
    trailing_stop=False
    use_exit_signal=True
    exit_profit_only=False
    ignore_roi_if_entry_signal=False

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        p=PARAMS
        dataframe["rsi"]=ta.RSI(dataframe,timeperiod=14)
        dataframe["atr"]=ta.ATR(dataframe,timeperiod=14)
        dataframe["atr_pct"]=dataframe["atr"]/dataframe["close"]
        dataframe["qv"]=dataframe["volume"]*dataframe["close"]
        dataframe["qv_med24"]=dataframe["qv"].rolling(24).median()
        dataframe["relvol"]=dataframe["qv"]/dataframe["qv_med24"]

        if FAMILY=="TS_MOMENTUM":
            f=int(p.get("emaFast",48)); s=int(p.get("emaSlow",120)); lb=int(p.get("retLookback",24))
            dataframe["ema_fast"]=ta.EMA(dataframe,timeperiod=f)
            dataframe["ema_slow"]=ta.EMA(dataframe,timeperiod=s)
            dataframe["ret_lb"]=dataframe["close"].pct_change(lb)

        elif FAMILY=="LIQUIDITY_REVERSAL":
            lb=int(p.get("retLookback",6)); zlb=int(p.get("zLookback",720))
            dataframe["ret_lb"]=dataframe["close"].pct_change(lb)
            dataframe["ret_mu"]=dataframe["ret_lb"].rolling(zlb).mean()
            dataframe["ret_sd"]=dataframe["ret_lb"].rolling(zlb).std(ddof=0)
            dataframe["z"]=(dataframe["ret_lb"]-dataframe["ret_mu"])/dataframe["ret_sd"].replace(0,pd.NA)
            dataframe["qv_week_med"]=dataframe["qv"].rolling(7*24).median()
            dataframe["volume_ratio"]=dataframe["qv"]/dataframe["qv_week_med"]
            dataframe["ema24"]=ta.EMA(dataframe,timeperiod=24)

        elif FAMILY=="VOLATILITY_BREAKOUT":
            lb=int(p.get("lookback",24)); clb=int(p.get("compressionLookback",72))
            dataframe["hh"]=dataframe["high"].rolling(lb).max().shift(1)
            dataframe["atr_rank"]=dataframe["atr_pct"].rolling(clb).rank(pct=True)

        elif FAMILY=="TREND_BREAKOUT":
            dataframe["ema_fast"]=ta.EMA(dataframe,timeperiod=int(p.get("fast",20)))
            dataframe["ema_slow"]=ta.EMA(dataframe,timeperiod=int(p.get("slow",60)))
            dataframe["hh"]=dataframe["high"].rolling(int(p.get("lookback",20))).max().shift(1)

        elif FAMILY=="MEAN_REVERSION":
            dataframe["mid"]=dataframe["close"].rolling(20).mean()
            dataframe["sd"]=dataframe["close"].rolling(20).std(ddof=0)
            dataframe["lower"]=dataframe["mid"]-float(p.get("bb",2.0))*dataframe["sd"]

        elif FAMILY=="VOLATILITY_MOMENTUM":
            dataframe["hh"]=dataframe["high"].rolling(int(p.get("lookback",20))).max().shift(1)
            dataframe["ema20"]=ta.EMA(dataframe,timeperiod=20)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        p=PARAMS
        dataframe["enter_long"]=0
        dataframe["enter_tag"]=None

        if FAMILY=="TS_MOMENTUM":
            cond=(
                (dataframe["close"]>dataframe["ema_fast"])
                &(dataframe["ema_fast"]>dataframe["ema_slow"])
                &(dataframe["ret_lb"]>float(p.get("retMin",0.02)))
                &dataframe["atr_pct"].between(float(p.get("atrMin",0.006)),float(p.get("atrMax",0.08)))
                &(dataframe["relvol"]>=float(p.get("relvol",0.8)))
                &(dataframe["volume"]>0)
            )
        elif FAMILY=="LIQUIDITY_REVERSAL":
            cond=(
                (dataframe["z"]<=float(p.get("zMax",-2.0)))
                &(dataframe["volume_ratio"]<=float(p.get("volumeRatioMax",1.10)))
                &(dataframe["rsi"]<=float(p.get("rsiMax",35)))
                &(dataframe["close"]<dataframe["ema24"])
                &(dataframe["volume"]>0)
            )
        elif FAMILY=="VOLATILITY_BREAKOUT":
            cond=(
                (dataframe["atr_rank"].shift(1)<=float(p.get("compressionPct",0.25)))
                &(dataframe["close"]>dataframe["hh"])
                &(dataframe["relvol"]>=float(p.get("relvol",1.5)))
                &dataframe["rsi"].between(float(p.get("rsiMin",55)),float(p.get("rsiMax",75)))
                &(dataframe["volume"]>0)
            )
        elif FAMILY=="TREND_BREAKOUT":
            cond=(dataframe["ema_fast"]>dataframe["ema_slow"])&(dataframe["close"]>dataframe["hh"])&(dataframe["relvol"]>=float(p.get("relvol",1.0)))&dataframe["rsi"].between(52,72)&(dataframe["volume"]>0)
        elif FAMILY=="MEAN_REVERSION":
            cond=(dataframe["close"]<dataframe["lower"])&(dataframe["rsi"]<=float(p.get("rsi_in",34)))&(dataframe["relvol"]>=0.75)&(dataframe["volume"]>0)
        elif FAMILY=="VOLATILITY_MOMENTUM":
            cond=(dataframe["close"]>dataframe["hh"])&(dataframe["relvol"]>=float(p.get("relvol",1.4)))&dataframe["rsi"].between(float(p.get("rsi_min",52)),74)&(dataframe["close"]>dataframe["ema20"])&(dataframe["volume"]>0)
        else:
            cond=(dataframe["volume"]<0)

        dataframe.loc[cond,["enter_long","enter_tag"]]=(1,FAMILY.lower())
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        p=PARAMS
        dataframe["exit_long"]=0
        dataframe["exit_tag"]=None
        if FAMILY=="MEAN_REVERSION":
            cond=(dataframe["close"]>=dataframe["mid"])|(dataframe["rsi"]>=float(p.get("rsi_out",50)))
            dataframe.loc[cond,["exit_long","exit_tag"]]=(1,"mean_reversion_done")
        elif FAMILY=="TREND_BREAKOUT":
            cond=(dataframe["ema_fast"]<dataframe["ema_slow"])|(dataframe["rsi"]<45)
            dataframe.loc[cond,["exit_long","exit_tag"]]=(1,"trend_failure")
        elif FAMILY=="VOLATILITY_MOMENTUM":
            cond=(dataframe["close"]<dataframe["ema20"])|(dataframe["rsi"]<45)
            dataframe.loc[cond,["exit_long","exit_tag"]]=(1,"vol_failure")
        return dataframe

    def custom_exit(self,pair,trade,current_time:datetime,current_rate,current_profit,**kwargs):
        hold=int(PARAMS.get("holdBars",0))
        if hold<=0:
            return None
        minutes=60 if TIMEFRAME=="1h" else 15
        age=(current_time-trade.open_date_utc).total_seconds()/60
        if age>=hold*minutes:
            return "time_exit"
        return None
