#!/usr/bin/env python3
"""Technical wrapper for Direct Depth20 V1: normalize all timestamp join keys to ns UTC.
No hypothesis, window, cost, or gate changes.
"""
from __future__ import annotations
import pandas as pd
import direct_depth20_round as core

_orig_load_monthly = core.load_monthly
_orig_event_features = core.event_features

def _ns(df, cols=('ts',)):
    out=df.copy()
    for c in cols:
        if c in out.columns:
            out[c]=pd.to_datetime(out[c],utc=True).astype('datetime64[ns, UTC]')
    return out

def load_monthly(symbol,month,market='spot'):
    return _ns(_orig_load_monthly(symbol,month,market),('ts',))

def event_features(path):
    x,q=_orig_event_features(path)
    if not x.empty: x=_ns(x,('ts',))
    return x,q

core.load_monthly=load_monthly
core.event_features=event_features

if __name__=='__main__':
    core.main()
