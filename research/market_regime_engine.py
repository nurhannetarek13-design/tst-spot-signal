#!/usr/bin/env python3
"""Point-in-time six-state market regime engine for Binance Spot research.

RESEARCH ONLY. It does not affect live trading.

Input CSV (hourly bars, long format):
  ts,symbol,close,quote_volume
Optional: high,low

The script fits all regime thresholds on the discovery period only, freezes them,
and then labels the full sample. This avoids using future quantiles to classify
past observations.
"""
from __future__ import annotations

import argparse,json
from pathlib import Path
import numpy as np
import pandas as pd

STATES=['STRONG_BULL','WEAK_BULL','SIDEWAYS_COMPRESSION','WEAK_BEAR','PANIC_HIGH_VOL_BEAR','POST_CRASH_RECOVERY']


def _load(path:Path)->pd.DataFrame:
    df=pd.read_csv(path)
    req={'ts','symbol','close','quote_volume'}
    missing=req-set(df.columns)
    if missing: raise SystemExit(f'missing columns: {sorted(missing)}')
    df=df.copy(); df['ts']=pd.to_datetime(df['ts'],utc=True,errors='coerce')
    df['symbol']=df['symbol'].astype(str).str.upper()
    for c in ['close','quote_volume']:
        df[c]=pd.to_numeric(df[c],errors='coerce')
    df=df.dropna(subset=['ts','symbol','close','quote_volume']).sort_values(['symbol','ts'])
    return df


def _features(df:pd.DataFrame)->pd.DataFrame:
    d=df.copy()
    g=d.groupby('symbol',group_keys=False)
    d['r1']=g['close'].pct_change(1)
    d['r6']=g['close'].pct_change(6)
    d['r24']=g['close'].pct_change(24)
    d['rv24']=g['r1'].transform(lambda s:s.rolling(24,min_periods=16).std())
    d['ema20']=g['close'].transform(lambda s:s.ewm(span=20,adjust=False,min_periods=20).mean())
    d['ema50']=g['close'].transform(lambda s:s.ewm(span=50,adjust=False,min_periods=40).mean())

    btc=d[d.symbol=='BTCUSDT'][['ts','r6','r24','rv24','ema20','ema50','close']].copy()
    btc=btc.rename(columns={c:f'btc_{c}' for c in btc.columns if c!='ts'})

    # Point-in-time liquidity universe: at each timestamp keep symbols whose
    # rolling 24h quote volume is above the cross-sectional median. This avoids
    # letting dead/illiquid tails dominate breadth.
    d['qv24']=g['quote_volume'].transform(lambda s:s.rolling(24,min_periods=8).sum())
    med=d.groupby('ts')['qv24'].transform('median')
    u=d[d['qv24']>=med].copy()
    xs=u.groupby('ts').agg(
        breadth24=('r24',lambda x:float(np.nanmean(np.asarray(x)>0))),
        breadth6=('r6',lambda x:float(np.nanmean(np.asarray(x)>0))),
        median24=('r24','median'),
        dispersion24=('r24','std'),
        universe_n=('symbol','nunique'),
    ).reset_index()
    out=btc.merge(xs,on='ts',how='left').sort_values('ts')
    out['breadth_delta6']=out['breadth6'].diff(6)
    out['btc_trend']=np.where(out['btc_ema20']>out['btc_ema50'],1,-1)
    return out.dropna(subset=['btc_r24','btc_rv24','breadth24']).reset_index(drop=True)


def fit_thresholds(f:pd.DataFrame,discovery_frac:float)->dict:
    n=max(100,int(len(f)*discovery_frac)); x=f.iloc[:n]
    q=lambda c,p:float(x[c].quantile(p))
    return {
        'fit_rows':int(n),'fit_end':str(x.ts.iloc[-1]),
        'btc_r24_p10':q('btc_r24',.10),'btc_r24_p35':q('btc_r24',.35),'btc_r24_p65':q('btc_r24',.65),
        'rv24_p35':q('btc_rv24',.35),'rv24_p80':q('btc_rv24',.80),
        'breadth_p20':q('breadth24',.20),'breadth_p40':q('breadth24',.40),'breadth_p60':q('breadth24',.60),'breadth_p70':q('breadth24',.70),
        'breadth_recovery_delta':max(.05,q('breadth_delta6',.65)),
    }


def classify(f:pd.DataFrame,t:dict)->pd.DataFrame:
    rows=[]; panic_until=-10_000
    for i,r in f.iterrows():
        panic=(r.btc_r24<=t['btc_r24_p10'] and r.btc_rv24>=t['rv24_p80'] and r.breadth24<=t['breadth_p20'])
        if panic:
            state='PANIC_HIGH_VOL_BEAR'; panic_until=i+72
        elif i<=panic_until and r.btc_r6>0 and r.breadth_delta6>=t['breadth_recovery_delta'] and r.breadth24>=t['breadth_p40']:
            state='POST_CRASH_RECOVERY'
        elif r.btc_trend>0 and r.btc_r24>=t['btc_r24_p65'] and r.breadth24>=t['breadth_p70']:
            state='STRONG_BULL'
        elif r.btc_trend>0 and r.btc_r24>0 and r.breadth24>=t['breadth_p60']:
            state='WEAK_BULL'
        elif r.btc_trend<0 and r.btc_r24<=t['btc_r24_p35'] and r.breadth24<=t['breadth_p40']:
            state='WEAK_BEAR'
        else:
            state='SIDEWAYS_COMPRESSION'
        row=r.to_dict(); row['regime']=state; rows.append(row)
    return pd.DataFrame(rows)


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True,type=Path); ap.add_argument('--output',required=True,type=Path)
    ap.add_argument('--thresholds',required=True,type=Path); ap.add_argument('--discovery-frac',type=float,default=.60)
    a=ap.parse_args(); df=_load(a.input); f=_features(df)
    if len(f)<300: raise SystemExit(f'insufficient hourly history: {len(f)} rows')
    t=fit_thresholds(f,a.discovery_frac); out=classify(f,t)
    a.output.parent.mkdir(parents=True,exist_ok=True); out.to_csv(a.output,index=False)
    a.thresholds.parent.mkdir(parents=True,exist_ok=True); a.thresholds.write_text(json.dumps(t,indent=2),encoding='utf-8')
    print(out['regime'].value_counts().to_dict())

if __name__=='__main__': main()
