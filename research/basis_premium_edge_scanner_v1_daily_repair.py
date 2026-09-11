#!/usr/bin/env python3
"""Data-ingest repair wrapper for frozen Round 8.

This does NOT change any Round 8 family, threshold, horizon, gate, declustering,
or statistical rule. It only replaces load_premium() so missing full-day gaps in
monthly Binance Vision premiumIndexKlines are recovered from the corresponding
daily archive before the original frozen main() executes.
"""
from __future__ import annotations
import importlib.util
import pathlib
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / 'basis_premium_edge_scanner_v1.py'
spec = importlib.util.spec_from_file_location('round8_frozen', SRC)
r8 = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(r8)


def repaired_load_premium(sym):
    frames = []
    for ym in r8.months():
        u = f'{r8.BASE}/monthly/premiumIndexKlines/{sym}/5m/{sym}-5m-{ym}.zip'
        frames.append(r8.zip_csv(u, r8.KLINE_COLS))

    x = pd.concat(frames, ignore_index=True)
    x['ts'] = r8._ts_from_open(x.open_time)
    x['premium'] = pd.to_numeric(x.close, errors='coerce')
    x = x.dropna(subset=['ts']).drop_duplicates('ts', keep='last').set_index('ts').sort_index()

    grid = pd.date_range(r8.START, r8.END - pd.Timedelta(minutes=5), freq='5min')
    missing = grid.difference(x.index)

    # Recover only complete missing UTC days from Binance Vision daily archive.
    # Partial-day gaps remain missing and therefore continue to fail coverage.
    recovered_days = []
    if len(missing):
        counts = pd.Series(1, index=missing).groupby(missing.date).sum()
        full_days = [str(day) for day, n in counts.items() if int(n) == 288]
        for day in full_days:
            u = f'{r8.BASE}/daily/premiumIndexKlines/{sym}/5m/{sym}-5m-{day}.zip'
            d = r8.zip_csv(u, r8.KLINE_COLS)
            d['ts'] = r8._ts_from_open(d.open_time)
            d['premium'] = pd.to_numeric(d.close, errors='coerce')
            d = d.dropna(subset=['ts'])[['ts', 'premium']].set_index('ts')
            x = pd.concat([x[['premium']], d]).sort_index()
            x = x[~x.index.duplicated(keep='last')]
            recovered_days.append(day)

    x = x.reindex(grid)
    remaining = grid.difference(x.index[x.premium.notna()])
    coverage = float(x.premium.notna().mean())
    r8.log(f'{sym} premium repair recoveredDays={recovered_days} remainingMissing={len(remaining)} coverage={coverage:.6f}')
    return x[['premium']], coverage


r8.load_premium = repaired_load_premium

if __name__ == '__main__':
    r8.main()
