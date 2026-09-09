#!/usr/bin/env python3
"""Build point-in-time cross-sectional opportunity features for calibration.

RESEARCH ONLY. This deliberately does NOT assign arbitrary point weights.
It produces statistically testable factors that feed calibrated_opportunity_model.

Input CSV, hourly long format:
  ts,symbol,close,quote_volume
Optional columns: taker_buy_quote_volume,total_quote_volume,spread_pct
"""
from __future__ import annotations
import argparse
from pathlib import Path
import numpy as np
import pandas as pd


def pct_rank(s:pd.Series)->pd.Series:
    return s.rank(pct=True,method='average')


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True,type=Path); ap.add_argument('--output',required=True,type=Path)
    a=ap.parse_args(); d=pd.read_csv(a.input)
    req={'ts','symbol','close','quote_volume'}; miss=req-set(d.columns)
    if miss: raise SystemExit(f'missing columns: {sorted(miss)}')
    d=d.copy(); d['ts']=pd.to_datetime(d.ts,utc=True,errors='coerce'); d['symbol']=d.symbol.astype(str).str.upper()
    for c in ['close','quote_volume','taker_buy_quote_volume','total_quote_volume','spread_pct']:
        if c in d: d[c]=pd.to_numeric(d[c],errors='coerce')
    d=d.dropna(subset=['ts','symbol','close','quote_volume']).sort_values(['symbol','ts'])
    g=d.groupby('symbol',group_keys=False)
    for h in [1,4,24]: d[f'ret_{h}h']=g.close.pct_change(h)
    d['accel_1h']=d['ret_1h']-g['ret_1h'].shift(1)
    d['qv24']=g.quote_volume.transform(lambda s:s.rolling(24,min_periods=8).sum())
    d['volume_ratio_1h']=d.quote_volume/g.quote_volume.transform(lambda s:s.rolling(24,min_periods=8).median())
    d['volatility_24h']=g['ret_1h'].transform(lambda s:s.rolling(24,min_periods=16).std())

    btc=d[d.symbol=='BTCUSDT'][['ts','ret_1h','ret_4h','ret_24h']].rename(columns={c:f'btc_{c}' for c in ['ret_1h','ret_4h','ret_24h']})
    d=d.merge(btc,on='ts',how='left')
    for h in [1,4,24]: d[f'rs_btc_{h}h']=d[f'ret_{h}h']-d[f'btc_ret_{h}h']

    # Point-in-time universe ranks. No future liquidity membership is used.
    for c in ['ret_1h','ret_4h','ret_24h','rs_btc_1h','rs_btc_4h','rs_btc_24h','accel_1h','volume_ratio_1h','qv24']:
        d[f'{c}_pct']=d.groupby('ts')[c].transform(pct_rank)
    d['xs_median_ret_1h']=d.groupby('ts')['ret_1h'].transform('median')
    d['xs_median_ret_4h']=d.groupby('ts')['ret_4h'].transform('median')
    d['rs_universe_1h']=d.ret_1h-d.xs_median_ret_1h
    d['rs_universe_4h']=d.ret_4h-d.xs_median_ret_4h

    if {'taker_buy_quote_volume','total_quote_volume'}<=set(d.columns):
        d['taker_buy_ratio']=d.taker_buy_quote_volume/d.total_quote_volume.replace(0,np.nan)
        d['taker_buy_ratio_pct']=d.groupby('ts').taker_buy_ratio.transform(pct_rank)

    # Eligibility flag only; final decision weight comes from the calibrated model.
    d['liquid_universe']=((d.qv24_pct>=.50)&(d.symbol!='BTCUSDT')).astype(int)
    keep=['ts','symbol','liquid_universe','ret_1h','ret_4h','ret_24h','rs_btc_1h','rs_btc_4h','rs_btc_24h',
          'rs_universe_1h','rs_universe_4h','accel_1h','volume_ratio_1h','volatility_24h','qv24']
    keep += [c for c in d.columns if c.endswith('_pct') and c not in keep]
    if 'taker_buy_ratio' in d: keep+=['taker_buy_ratio']
    if 'spread_pct' in d: keep+=['spread_pct']
    keep=list(dict.fromkeys([c for c in keep if c in d.columns]))
    out=d[keep].dropna(subset=['ret_1h','ret_4h','rs_btc_1h','rs_btc_4h'])
    a.output.parent.mkdir(parents=True,exist_ok=True); out.to_csv(a.output,index=False)
    print({'rows':len(out),'symbols':out.symbol.nunique(),'liquid_rows':int(out.liquid_universe.sum())})

if __name__=='__main__': main()
