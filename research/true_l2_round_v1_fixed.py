#!/usr/bin/env python3
"""Timestamp-normalized launcher for frozen True L2 Round V1.
No research definition, threshold, split, cost or gate changes.
"""
import numpy as np
import pandas as pd
import research.true_l2_round_v1 as r


def ns(s):
    return pd.Series(pd.to_datetime(s, utc=True), index=getattr(s, 'index', None)).astype('datetime64[ns, UTC]')


def prepare_fixed(l2,tr,spot):
    x=l2.copy(); x['ts']=pd.to_datetime(x.ts_ms,unit='ms',utc=True).astype('datetime64[ns, UTC]'); x=x.drop(columns='ts_ms').sort_values('ts')
    tr=tr.copy(); tr['ts']=pd.to_datetime(tr['ts'],utc=True).astype('datetime64[ns, UTC]')
    x=pd.merge_asof(x,tr.sort_values('ts'),on='ts',direction='backward',tolerance=pd.Timedelta('5min'))
    sp=spot.copy(); sp['ts']=pd.to_datetime(sp['ts'],utc=True).astype('datetime64[ns, UTC]'); sp['entry_ts']=(sp.ts-pd.Timedelta(minutes=1)).astype('datetime64[ns, UTC]')
    right=sp[['entry_ts','open']].rename(columns={'open':'entry'}).sort_values('entry_ts')
    x=pd.merge_asof(x.sort_values('ts'),right,left_on='ts',right_on='entry_ts',direction='forward',tolerance=pd.Timedelta('2min'))
    for h in r.HORIZONS_MIN:
        target=sp[['ts','open']].copy(); target['lookup_ts']=(target.ts-pd.Timedelta(minutes=h+1)).astype('datetime64[ns, UTC]')
        target=target[['lookup_ts','open']].rename(columns={'open':f'exit_{h}m'}).sort_values('lookup_ts')
        x=pd.merge_asof(x.sort_values('ts'),target,left_on='ts',right_on='lookup_ts',direction='nearest',tolerance=pd.Timedelta('1min'))
        x[f'fwd_{h}m']=x[f'exit_{h}m']/x.entry-1
    for c in ['bbo_ofi_5m','micro_dev_bps','imb_1','imb_5','imb_10','aggr_sell_ratio']:
        x[c+'__z' if False else c+'_z']=r.roll_z(x[c].astype(float))
    x['imb10_chg']=x.imb_10.diff(); x['bid10_pct']=x.bid_depth_10.pct_change().replace([np.inf,-np.inf],np.nan); x['ask10_pct']=x.ask_depth_10.pct_change().replace([np.inf,-np.inf],np.nan)
    x['ret5']=x.entry.pct_change(); x['ret5_z']=r.roll_z(x.ret5)
    x['sell_quote_z']=r.roll_z(x.sell_quote.astype(float))
    x['impact_per_sell']=x.ret5.abs()/x.sell_quote.replace(0,np.nan)
    x['impact_q20']=x.impact_per_sell.rolling(288,min_periods=144).quantile(.2)
    return x

r.prepare=prepare_fixed
if __name__=='__main__':
    r.main()
