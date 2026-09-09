from __future__ import annotations

"""Independent forward-validation guard for the calibrated EV model.

This process consumes only completed point-in-time candidate outcomes collected
by the live shadow telemetry. It never places orders. Its sole job is to decide
whether the current EV feature version has enough forward evidence to be allowed
as a hard live gate.
"""

import json
import os
import statistics
import time
from pathlib import Path

import shadow_ev_model as m

APPROVAL_PATH = Path(os.getenv('TST_EV_LIVE_APPROVAL_PATH', '/data/tst_ev_live_approval.json'))
REPORT_PATH = Path(os.getenv('TST_SHADOW_EV_REPORT_PATH', '/data/tst_shadow_ev_report.json'))
MIN_FORWARD = max(400, int(os.getenv('EV_PROMOTION_MIN_FORWARD_SAMPLE', '600')))
MIN_FOLD_TEST = max(40, int(os.getenv('EV_PROMOTION_MIN_FOLD_TEST', '60')))
FOLDS = max(3, min(6, int(os.getenv('EV_PROMOTION_FOLDS', '4'))))
EXTRA_STRESS_COST_PCT = max(0.0, float(os.getenv('EV_PROMOTION_EXTRA_STRESS_COST_PCT', '0.28')))
POLL_SEC = max(300, int(os.getenv('EV_PROMOTION_POLL_SEC', '900')))


def _atomic(row: dict) -> None:
    APPROVAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = APPROVAL_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(row, separators=(',', ':'), ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, APPROVAL_PATH)


def _read_report() -> dict:
    try:
        x = json.loads(REPORT_PATH.read_text(encoding='utf-8'))
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def _fit_predict(train: list[dict], test: list[dict]) -> tuple[list[float], list[float]]:
    schema = m._fit_schema(train)
    xtr = [m._vec(r, schema) for r in train]
    xte = [m._vec(r, schema) for r in test]
    y = [int(r['tp_before_sl']) for r in train]
    if len(set(y)) < 2 or min(sum(y), len(y) - sum(y)) < 25:
        return [], []
    pw = m._fit_logistic(xtr, y)
    # Calibration is deliberately internal to each fold. Use the final 20% of
    # the training window as calibration, then refit regression on the full
    # expanding training window.
    cut = max(1, int(len(train) * 0.80))
    cal = train[cut:]
    platt = [0.0, 1.0]
    if len(cal) >= 30:
        xcal = [m._vec(r, schema) for r in cal]
        cy = [int(r['tp_before_sl']) for r in cal]
        if len(set(cy)) == 2:
            raw = [m._sigmoid(m._dot(pw, x)) for x in xcal]
            platt = m._fit_platt(raw, cy)
    probs = [m._calibrate(m._sigmoid(m._dot(pw, x)), platt) for x in xte]
    nw = m._fit_linear(xtr, [float(r['sim_net_pct']) for r in train])
    evs = [m._dot(nw, x) for x in xte]
    return probs, evs


def _fold_metrics(test: list[dict], probs: list[float], evs: list[float]) -> dict:
    if not test or not probs or not evs:
        return {'n': len(test), 'pass': False, 'reason': 'fit-unavailable'}
    k = max(10, int(len(test) * 0.20))
    ids = sorted(range(len(test)), key=lambda i: evs[i], reverse=True)[:k]
    stressed = [float(test[i]['sim_net_pct']) - EXTRA_STRESS_COST_PCT for i in ids]
    p_actual = [int(test[i]['tp_before_sl']) for i in ids]
    p_pred = [float(probs[i]) for i in ids]
    mean = sum(stressed) / len(stressed)
    median = statistics.median(stressed)
    hit = sum(1 for x in stressed if x > 0) / len(stressed)
    cal_gap = abs((sum(p_pred) / len(p_pred)) - (sum(p_actual) / len(p_actual)))
    ci = m._bootstrap_mean_ci(stressed)
    lo = ci[0]
    passed = mean > 0.0 and median > -0.05 and hit >= 0.50 and cal_gap <= 0.12 and lo is not None and lo > -0.10
    return {
        'n': len(test), 'top_n': len(stressed), 'mean_stressed_net_pct': round(mean, 4),
        'median_stressed_net_pct': round(median, 4), 'hit_rate_stressed': round(hit, 4),
        'probability_calibration_gap_top': round(cal_gap, 4), 'bootstrap95': ci, 'pass': passed,
    }


def build() -> dict:
    all_rows = [r for r in m._rows() if r.get('tp_before_sl') in {0, 1} and r.get('sim_net_pct') is not None]
    report = _read_report()
    base = {
        'version': 1,
        'feature_version': m.FEATURE_VERSION,
        'generated_at': time.time(),
        'minimum_forward_sample': MIN_FORWARD,
        'forward_sample': len(all_rows),
        'extra_stress_cost_pct': EXTRA_STRESS_COST_PCT,
        'automatic_order_execution': False,
    }
    if len(all_rows) < MIN_FORWARD:
        return {**base, 'status': 'WAITING_FORWARD_SAMPLE', 'approved_at': None, 'folds': []}
    if not bool(report.get('evidence_pass')):
        return {**base, 'status': 'WAITING_BASE_MODEL_EVIDENCE', 'approved_at': None, 'folds': []}

    # Expanding-window walk-forward. First 40% is the initial training block;
    # remaining observations are divided into chronological untouched folds.
    n = len(all_rows)
    initial = max(240, int(n * 0.40))
    remaining = n - initial
    fold_size = max(MIN_FOLD_TEST, remaining // FOLDS)
    folds = []
    cursor = initial
    while cursor < n and len(folds) < FOLDS:
        end = n if len(folds) == FOLDS - 1 else min(n, cursor + fold_size)
        train = all_rows[:cursor]
        test = all_rows[cursor:end]
        if len(test) < MIN_FOLD_TEST:
            break
        probs, evs = _fit_predict(train, test)
        fm = _fold_metrics(test, probs, evs)
        fm.update({'train_n': len(train), 'start_ts': test[0].get('source_ts') or test[0].get('ts'), 'end_ts': test[-1].get('source_ts') or test[-1].get('ts')})
        folds.append(fm)
        cursor = end

    enough_folds = len(folds) >= 3
    fold_pass = enough_folds and all(bool(x.get('pass')) for x in folds)
    # The base report already requires symbol holdout and >=3 regime coverage.
    holdout = report.get('symbol_holdout_test') or {}
    holdout_ok = int(holdout.get('n') or 0) >= 20 and float(holdout.get('top20_predicted_ev_mean_net_pct') or -999) > 0
    prob = report.get('probability_test') or {}
    calibration_ok = float(prob.get('brier_skill') or -1) > 0.02 and float(prob.get('calibration_mae') or 99) <= 0.10
    approved = fold_pass and holdout_ok and calibration_ok
    return {
        **base,
        'status': 'APPROVED' if approved else 'NOT_APPROVED',
        'approved_at': time.time() if approved else None,
        'folds': folds,
        'fold_pass': fold_pass,
        'symbol_holdout_pass': holdout_ok,
        'calibration_pass': calibration_ok,
        'base_evidence_pass': bool(report.get('evidence_pass')),
    }


def run_once() -> dict:
    row = build()
    _atomic(row)
    print(f"[ev-validation] status={row['status']} n={row['forward_sample']} folds={len(row.get('folds') or [])} feature={row['feature_version']}", flush=True)
    return row


def main() -> None:
    print(f'[ev-validation] ONLINE min_forward={MIN_FORWARD} folds={FOLDS} extra_stress={EXTRA_STRESS_COST_PCT:.2f}%', flush=True)
    while True:
        started = time.time()
        try:
            run_once()
        except Exception as exc:
            print(f'[ev-validation] warning {type(exc).__name__}: {str(exc)[:180]}', flush=True)
        time.sleep(max(60.0, POLL_SEC - (time.time() - started)))


if __name__ == '__main__':
    run_once() if '--once' in __import__('sys').argv else main()
