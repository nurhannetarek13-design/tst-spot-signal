import json
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd
from pandas import DataFrame
from freqtrade.strategy import IStrategy

CANDIDATE_PATHS=[
    Path("/freqtrade/user_data/candidate-manifest.json"),
    Path("freqtrade/user_data/candidate-manifest.json"),
    Path("validation/fusion/frozen-parity-candidate.json"),
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


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=int(n), adjust=False).mean()


def rsi_wilder(series: pd.Series, n: int = 14) -> pd.Series:
    d=series.diff()
    gain=d.clip(lower=0)
    loss=-d.clip(upper=0)
    avg_gain=gain.ewm(alpha=1/n,adjust=False).mean()
    avg_loss=loss.ewm(alpha=1/n,adjust=False).mean()
    rs=avg_gain/avg_loss.replace(0,np.nan)
    return (100-100/(1+rs)).fillna(50.0)


def atr_pct_sma(dataframe: DataFrame, n: int = 14) -> pd.Series:
    pc=dataframe["close"].shift(1)
    tr=pd.concat([
        (dataframe["high"]-dataframe["low"]).abs(),
        (dataframe["high"]-pc).abs(),
        (dataframe["low"]-pc).abs(),
    ],axis=1).max(axis=1)
    return tr.rolling(n).mean()/dataframe["close"]


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

    def informative_pairs(self):
        if FAMILY=="CROSS_CRYPTO_LEAD_LAG":
            return [("BTC/USDT","1h"),("ETH/USDT","1h"),("SOL/USDT","1h")]
        return []

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        p=PARAMS
        dataframe["rsi"]=rsi_wilder(dataframe["close"],14)
        dataframe["atr_pct"]=atr_pct_sma(dataframe,14)
        dataframe["qv"]=dataframe["volume"]*dataframe["close"]
        dataframe["qv_med24"]=dataframe["qv"].rolling(24).median()
        dataframe["relvol"]=dataframe["qv"]/dataframe["qv_med24"].replace(0,np.nan)

        if FAMILY=="CROSS_CRYPTO_LEAD_LAG":
            leaders=[]
            if self.dp:
                for pair in ["BTC/USDT","ETH/USDT","SOL/USDT"]:
                    inf=self.dp.get_pair_dataframe(pair=pair,timeframe="1h").copy()
                    inf["leader_ret3"]=inf["close"].pct_change(3)
                    leaders.append(inf[["date","leader_ret3"]].rename(columns={"leader_ret3":f"lead_{pair.split('/')[0]}"}))
                for z in leaders:
                    dataframe=dataframe.merge(z,on="date",how="left")
                cols=[x for x in ["lead_BTC","lead_ETH","lead_SOL"] if x in dataframe.columns]
                dataframe["leader3"]=dataframe[cols].mean(axis=1) if cols else 0.0
            else:
                dataframe["leader3"]=0.0
            dataframe["ret3"]=dataframe["close"].pct_change(3)
            dataframe["gap"]=dataframe["leader3"]-dataframe["ret3"]
            dataframe["ema_fast"]=ema(dataframe["close"],int(p.get("emaFast",24)))

        elif FAMILY=="TS_MOMENTUM":
            f=int(p.get("emaFast",48)); s=int(p.get("emaSlow",120)); lb=int(p.get("retLookback",24))
            dataframe["ema_fast"]=ema(dataframe["close"],f)
            dataframe["ema_slow"]=ema(dataframe["close"],s)
            dataframe["ret_lb"]=dataframe["close"].pct_change(lb)

        elif FAMILY=="LIQUIDITY_REVERSAL":
            lb=int(p.get("retLookback",6)); zlb=int(p.get("zLookback",720))
            dataframe["ret_lb"]=dataframe["close"].pct_change(lb)
            dataframe["ret_mu"]=dataframe["ret_lb"].rolling(zlb).mean()
            dataframe["ret_sd"]=dataframe["ret_lb"].rolling(zlb).std(ddof=0)
            dataframe["z"]=(dataframe["ret_lb"]-dataframe["ret_mu"])/dataframe["ret_sd"].replace(0,np.nan)
            dataframe["qv_week_med"]=dataframe["qv"].rolling(7*24).median()
            dataframe["volume_ratio"]=dataframe["qv"]/dataframe["qv_week_med"].replace(0,np.nan)
            dataframe["ema24"]=ema(dataframe["close"],24)

        elif FAMILY=="VOLATILITY_BREAKOUT":
            lb=int(p.get("lookback",24)); clb=int(p.get("compressionLookback",72))
            dataframe["hh"]=dataframe["high"].rolling(lb).max().shift(1)
            dataframe["atr_rank"]=dataframe["atr_pct"].rolling(clb).rank(pct=True)

        elif FAMILY=="TREND_BREAKOUT":
            dataframe["ema_fast"]=ema(dataframe["close"],int(p.get("fast",20)))
            dataframe["ema_slow"]=ema(dataframe["close"],int(p.get("slow",60)))
            dataframe["hh"]=dataframe["high"].rolling(int(p.get("lookback",20))).max().shift(1)

        elif FAMILY=="MEAN_REVERSION":
            dataframe["mid"]=dataframe["close"].rolling(20).mean()
            dataframe["sd"]=dataframe["close"].rolling(20).std(ddof=0)
            dataframe["lower"]=dataframe["mid"]-float(p.get("bb",2.0))*dataframe["sd"]

        elif FAMILY=="VOLATILITY_MOMENTUM":
            dataframe["hh"]=dataframe["high"].rolling(int(p.get("lookback",20))).max().shift(1)
            dataframe["ema20"]=ema(dataframe["close"],20)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        p=PARAMS
        dataframe["enter_long"]=0
        dataframe["enter_tag"]=None

        if FAMILY=="CROSS_CRYPTO_LEAD_LAG":
            cond=(
                (dataframe["leader3"]>=float(p.get("leaderRetMin",0.012)))
                &(dataframe["gap"]>=float(p.get("gapMin",0.008)))
                &(dataframe["ret3"]>float(p.get("altRetMin",-0.02)))
                &(dataframe["close"]>dataframe["ema_fast"])
                &(dataframe["relvol"]>=float(p.get("relvol",0.9)))
                &dataframe["rsi"].between(float(p.get("rsiMin",42)),float(p.get("rsiMax",70)))
                &(dataframe["volume"]>0)
            )
        elif FAMILY=="TS_MOMENTUM":
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
        if hold<=0:return None
        minutes=60 if TIMEFRAME=="1h" else 15
        age=(current_time-trade.open_date_utc).total_seconds()/60
        if age>=hold*minutes:return "time_exit"
        return None
