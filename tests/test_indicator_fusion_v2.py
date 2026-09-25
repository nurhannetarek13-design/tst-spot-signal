import numpy as np, pandas as pd
from research.indicator_fusion_v2 import DEFAULT_PARAMS_V2, compute_indicator_fusion_v2, contract_v2, variance_ratio_series
from research.indicator_fusion_v2_discovery import era_cut_index

def frame(n=320):
    idx=pd.date_range("2025-01-01",periods=n,freq="h",tz="UTC")
    r=np.asarray(([.008,-.004,.007,-.003]*100)[:n]); c=np.cumprod(1+r); o=np.r_[1.0,c[:-1]]
    h=np.maximum(o,c)*1.001; l=np.minimum(o,c)*.999
    qv=np.full(n,2_000_000.0); qv[::5]=3_000_000.0
    tq=qv*np.asarray(([.51,.57,.53,.61,.56]*100)[:n])
    return pd.DataFrame({"open":o,"high":h,"low":l,"close":c,"volume":qv/c,"quote_volume":qv,"taker_quote":tq},index=idx)

def test_contract_fail_closed():
    c=contract_v2(); assert c["liveTrading"] is False and c["automaticPromotion"] is False and c["parameterSearch"] is False

def test_variance_ratio_series_shapes():
    d=frame(); x=variance_ratio_series(d.close,96,4); assert len(x)==len(d); assert np.isfinite(x.dropna()).all()

def test_event_required_for_entry():
    d=frame()
    p={**DEFAULT_PARAMS_V2,"scoreMin":0,"familiesMin":0,"varianceRatioMin":-999,"emaTrend":20,"flowLookback":24,"atrRankLookback":24,"breakoutLookback":12,"minRollingQuoteVolume24h":1}
    x=compute_indicator_fusion_v2(d,p)
    assert ((x["enter"]) <= (x["event"])).all()
    assert ((x["enter"]) <= (x["regime_ok"])).all()

def test_weak_flow_veto():
    d=frame(); d.loc[d.index[-1],"taker_quote"]=d.quote_volume.iloc[-1]*.40
    p={**DEFAULT_PARAMS_V2,"emaTrend":20,"flowLookback":24,"atrRankLookback":24,"breakoutLookback":12,"minRollingQuoteVolume24h":1}
    x=compute_indicator_fusion_v2(d,p); assert bool(x.hard_veto.iloc[-1]); assert not bool(x.enter.iloc[-1])


def test_era_cut_index_is_unit_safe():
    idx=pd.date_range("2025-01-01",periods=10,freq="h",tz="UTC")
    cutoff=idx[6]
    assert era_cut_index(idx,cutoff)==6
