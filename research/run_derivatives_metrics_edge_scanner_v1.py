#!/usr/bin/env python3
"""Execution-compatibility wrapper for frozen Round 7 scanner.

Normalizes pandas merge_asof datetime key precision only. It does not alter
symbols, event definitions, thresholds, horizons, gates, or research logic.
"""
import pandas as pd

_ORIG_MERGE_ASOF = pd.merge_asof

def _merge_asof_same_datetime_unit(left, right, *args, **kwargs):
    on = kwargs.get('on')
    if on and on in left.columns and on in right.columns:
        left = left.copy()
        right = right.copy()
        if isinstance(left[on].dtype, pd.DatetimeTZDtype) and isinstance(right[on].dtype, pd.DatetimeTZDtype):
            left[on] = left[on].astype('datetime64[us, UTC]')
            right[on] = right[on].astype('datetime64[us, UTC]')
    return _ORIG_MERGE_ASOF(left, right, *args, **kwargs)

pd.merge_asof = _merge_asof_same_datetime_unit

import derivatives_metrics_edge_scanner_v1 as scanner

scanner.main()
