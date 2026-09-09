#!/usr/bin/env python3
"""Calibrated probability + expected-value research model for TST Spot.

This module is RESEARCH ONLY. It never places orders and is intentionally not
imported by the live bot. Its purpose is to replace hand-assigned score weights
with frozen, out-of-sample calibrated estimates after sufficient data exists.

Required input CSV columns:
  ts,symbol,setup_type,regime,tp_before_sl,net_return_pct,mfe_pct,mae_pct,holding_min
Additional numeric columns are treated as candidate features.

The split is chronological: discovery -> calibration -> untouched test. No
random shuffle is allowed. A separate symbol-holdout report is also produced to
catch cross-sectional overfitting.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import brier_score_loss, log_loss, mean_absolute_error, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, RobustScaler

META = {
    'ts','symbol','setup_type','regime','tp_before_sl','net_return_pct',
    'mfe_pct','mae_pct','holding_min','split','event_id','decision','reason',
}


def _load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = {'ts','symbol','setup_type','regime','tp_before_sl','net_return_pct'} - set(df.columns)
    if missing:
        raise SystemExit(f'missing required columns: {sorted(missing)}')
    df = df.copy()
    df['ts'] = pd.to_datetime(df['ts'], utc=True, errors='coerce')
    df = df.dropna(subset=['ts','symbol','tp_before_sl','net_return_pct']).sort_values('ts')
    df['tp_before_sl'] = pd.to_numeric(df['tp_before_sl'], errors='coerce')
    df['net_return_pct'] = pd.to_numeric(df['net_return_pct'], errors='coerce')
    df = df.dropna(subset=['tp_before_sl','net_return_pct'])
    df = df[df['tp_before_sl'].isin([0,1])]
    return df.reset_index(drop=True)


def _features(df: pd.DataFrame) -> tuple[list[str], list[str]]:
    categorical = [c for c in ['setup_type','regime'] if c in df.columns]
    numeric = []
    for c in df.columns:
        if c in META or c in categorical:
            continue
        x = pd.to_numeric(df[c], errors='coerce')
        if x.notna().mean() >= 0.50 and x.nunique(dropna=True) >= 3:
            numeric.append(c)
    if not numeric:
        raise SystemExit('no usable numeric features found')
    return numeric, categorical


def _preprocessor(numeric: list[str], categorical: list[str]) -> ColumnTransformer:
    num = Pipeline([
        ('impute', SimpleImputer(strategy='median')),
        ('scale', RobustScaler()),
    ])
    cat = Pipeline([
        ('impute', SimpleImputer(strategy='most_frequent')),
        ('onehot', OneHotEncoder(handle_unknown='ignore')),
    ])
    return ColumnTransformer([('num', num, numeric), ('cat', cat, categorical)])


def _split(df: pd.DataFrame, discovery=0.60, calibration=0.20):
    n = len(df)
    a = max(1, int(n * discovery))
    b = max(a + 1, int(n * (discovery + calibration)))
    if n - b < 50:
        raise SystemExit('untouched test sample too small (<50 rows)')
    return df.iloc[:a], df.iloc[a:b], df.iloc[b:]


def _calibration_table(y: np.ndarray, p: np.ndarray, bins=10):
    out = []
    edges = np.linspace(0, 1, bins + 1)
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (p >= lo) & ((p < hi) if i < bins-1 else (p <= hi))
        if not mask.any():
            continue
        out.append({
            'bin_lo': round(float(lo),3), 'bin_hi': round(float(hi),3),
            'n': int(mask.sum()),
            'predicted': round(float(p[mask].mean()),4),
            'observed': round(float(y[mask].mean()),4),
        })
    return out


def _metrics(y, p, net_actual, net_pred):
    row = {
        'n': int(len(y)),
        'base_rate': float(np.mean(y)),
        'brier': float(brier_score_loss(y,p)),
        'logloss': float(log_loss(y,p,labels=[0,1])),
        'net_mae_pct': float(mean_absolute_error(net_actual,net_pred)),
        'mean_actual_net_pct': float(np.mean(net_actual)),
        'mean_pred_net_pct': float(np.mean(net_pred)),
    }
    try:
        row['auc'] = float(roc_auc_score(y,p))
    except Exception:
        row['auc'] = None
    return row


def train(df: pd.DataFrame, min_sample: int = 300) -> tuple[dict, pd.DataFrame]:
    if len(df) < min_sample:
        raise SystemExit(f'reject: sample {len(df)} < minimum {min_sample}')
    numeric, categorical = _features(df)
    discovery, calibration, test = _split(df)
    cols = numeric + categorical

    base = Pipeline([
        ('pre', _preprocessor(numeric,categorical)),
        ('model', LogisticRegression(C=0.5,max_iter=3000,class_weight='balanced')),
    ])
    base.fit(discovery[cols], discovery['tp_before_sl'].astype(int))

    # Freeze feature transformation/coefficients on discovery; calibration sees
    # the separate calibration period only. Test remains untouched.
    calibrated = CalibratedClassifierCV(base, method='isotonic', cv='prefit')
    calibrated.fit(calibration[cols], calibration['tp_before_sl'].astype(int))

    ev_model = Pipeline([
        ('pre', _preprocessor(numeric,categorical)),
        ('model', Ridge(alpha=10.0)),
    ])
    ev_model.fit(pd.concat([discovery, calibration])[cols], pd.concat([discovery, calibration])['net_return_pct'])

    p = calibrated.predict_proba(test[cols])[:,1]
    ev = ev_model.predict(test[cols])
    y = test['tp_before_sl'].astype(int).to_numpy()
    actual = test['net_return_pct'].to_numpy(float)

    report = {
        'status': 'RESEARCH_ONLY',
        'model_family': 'logistic+isotonic_probability / ridge_net_EV',
        'rows_total': int(len(df)),
        'rows_discovery': int(len(discovery)),
        'rows_calibration': int(len(calibration)),
        'rows_untouched_test': int(len(test)),
        'numeric_features': numeric,
        'categorical_features': categorical,
        'untouched_test': _metrics(y,p,actual,ev),
        'calibration_bins': _calibration_table(y,p),
    }

    scored = test[['ts','symbol','setup_type','regime','tp_before_sl','net_return_pct']].copy()
    scored['p_tp_before_sl'] = p
    scored['expected_net_pct'] = ev

    # Cross-symbol holdout stress: deterministic final 20% of symbols by name.
    symbols = sorted(df['symbol'].astype(str).unique())
    cut = max(1, int(len(symbols) * 0.8))
    held_symbols = set(symbols[cut:])
    symbol_holdout = test[test['symbol'].astype(str).isin(held_symbols)]
    if len(symbol_holdout) >= 30:
        hp = calibrated.predict_proba(symbol_holdout[cols])[:,1]
        hev = ev_model.predict(symbol_holdout[cols])
        report['symbol_holdout_test'] = _metrics(
            symbol_holdout['tp_before_sl'].astype(int).to_numpy(), hp,
            symbol_holdout['net_return_pct'].to_numpy(float), hev,
        )
        report['symbol_holdout_symbols'] = sorted(held_symbols)
    else:
        report['symbol_holdout_test'] = {'status':'INSUFFICIENT_SAMPLE','n':int(len(symbol_holdout))}

    # Research acceptance gate. This does NOT authorize live use; it only tells
    # the next validation stage whether the model is worth walk-forward testing.
    m = report['untouched_test']
    auc_ok = (m.get('auc') is not None and m['auc'] >= 0.56)
    calibration_ok = m['brier'] < (m['base_rate'] * (1-m['base_rate']))
    ev_ok = m['net_mae_pct'] <= max(0.75, float(np.std(actual)))
    report['discovery_gate'] = {
        'pass': bool(auc_ok and calibration_ok and ev_ok),
        'auc_ge_0_56': bool(auc_ok),
        'brier_better_than_base': bool(calibration_ok),
        'ev_error_sane': bool(ev_ok),
        'note': 'PASS means proceed to walk-forward/cost stress; never direct-live approval.',
    }
    return report, scored


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True, type=Path)
    ap.add_argument('--report', required=True, type=Path)
    ap.add_argument('--scores', type=Path)
    ap.add_argument('--min-sample', type=int, default=300)
    args = ap.parse_args()
    df = _load(args.input)
    report, scored = train(df, args.min_sample)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
    if args.scores:
        args.scores.parent.mkdir(parents=True, exist_ok=True)
        scored.to_csv(args.scores,index=False)
    print(json.dumps(report['discovery_gate'],indent=2))


if __name__ == '__main__':
    main()
