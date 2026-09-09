from __future__ import annotations

"""Calibrated probability/EV research model for candidate outcomes.

Research-only. This process NEVER places orders and NEVER changes the live gate.
It trains only after a minimum point-in-time sample exists, uses chronological
train/calibration/test partitions, reports symbol-holdout behavior, and refuses
automatic promotion even if metrics pass.
"""

import hashlib
import json
import math
import os
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path

OUTCOME_PATH = Path(os.getenv('TST_OUTCOME_PATH', '/data/tst_candidate_outcomes.jsonl'))
MODEL_PATH = Path(os.getenv('TST_SHADOW_EV_MODEL_PATH', '/data/tst_shadow_ev_model.json'))
REPORT_PATH = Path(os.getenv('TST_SHADOW_EV_REPORT_PATH', '/data/tst_shadow_ev_report.json'))
POLL_SEC = max(300, int(os.getenv('SHADOW_EV_POLL_SEC', '900')))
MIN_SAMPLE = max(300, int(os.getenv('SHADOW_EV_MIN_SAMPLE', '300')))
MIN_TEST = max(60, int(os.getenv('SHADOW_EV_MIN_TEST', '80')))
RIDGE = max(1e-5, float(os.getenv('SHADOW_EV_RIDGE', '0.10')))
BOOTSTRAP_N = max(200, min(2000, int(os.getenv('SHADOW_EV_BOOTSTRAP_N', '500'))))

NUMERIC_FEATURES = [
    'score', 'risk_pct', 'reward_pct', 'breadth_1h', 'breadth_4h',
    'rs_vs_btc_4h', 'r1h_pct_rank', 'r4h_pct_rank', 'rs_btc4h_pct_rank',
    'accel1h_pct_rank', 'volume_expansion_pct_rank', 'liquidity_pct_rank',
    'opportunity_pct_shadow',
]
LANES = ['NORMAL', 'MID', 'EXPLOSIVE', 'EXTREME']
REGIMES = ['STRONG_BULL', 'WEAK_BULL', 'SIDEWAYS_COMPRESSION', 'WEAK_BEAR', 'PANIC_HIGH_VOL_BEAR', 'POST_CRASH_RECOVERY']
FEATURE_VERSION = hashlib.sha256(('|'.join(NUMERIC_FEATURES + LANES + REGIMES)).encode()).hexdigest()[:12]


def _atomic(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(row, separators=(',', ':'), ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def _f(row: dict, key: str):
    try:
        x = float(row.get(key))
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _rows() -> list[dict]:
    if not OUTCOME_PATH.exists():
        return []
    latest = {}
    try:
        lines = OUTCOME_PATH.read_text(encoding='utf-8').splitlines()[-50000:]
    except Exception:
        return []
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or not row.get('event_id') or row.get('complete') is not True:
                continue
            if _f(row, 'sim_net_pct') is None:
                continue
            latest[str(row['event_id'])] = row
        except Exception:
            continue
    rows = list(latest.values())
    rows.sort(key=lambda r: float(r.get('source_ts') or r.get('ts') or 0))
    return rows


def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def _fit_schema(rows: list[dict]) -> dict:
    med = {}
    mean = {}
    sd = {}
    for key in NUMERIC_FEATURES:
        vals = [_f(r, key) for r in rows]
        vals = [x for x in vals if x is not None]
        m = _median(vals)
        filled = [x if x is not None else m for x in [_f(r, key) for r in rows]]
        mu = sum(filled) / len(filled) if filled else 0.0
        var = sum((x - mu) ** 2 for x in filled) / max(1, len(filled) - 1)
        med[key] = m
        mean[key] = mu
        sd[key] = max(math.sqrt(var), 1e-6)
    return {'median': med, 'mean': mean, 'sd': sd}


def _vec(row: dict, schema: dict) -> list[float]:
    x = [1.0]
    for key in NUMERIC_FEATURES:
        v = _f(row, key)
        if v is None:
            v = schema['median'][key]
        x.append((v - schema['mean'][key]) / schema['sd'][key])
    lane = str(row.get('lane') or '').upper()
    x.extend(1.0 if lane == k else 0.0 for k in LANES[1:])
    regime = str(row.get('regime') or '').upper()
    x.extend(1.0 if regime == k else 0.0 for k in REGIMES[1:])
    return x


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-min(z, 40.0))
        return 1.0 / (1.0 + ez)
    ez = math.exp(max(z, -40.0))
    return ez / (1.0 + ez)


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _fit_logistic(X: list[list[float]], y: list[int], ridge: float = RIDGE, steps: int = 1200) -> list[float]:
    if not X:
        return []
    p = len(X[0]); w = [0.0] * p
    # Conservative full-batch gradient descent with decaying learning rate.
    for step in range(steps):
        g = [0.0] * p
        for xi, yi in zip(X, y):
            pr = _sigmoid(_dot(w, xi))
            err = pr - yi
            for j, v in enumerate(xi):
                g[j] += err * v
        n = max(1, len(X))
        lr = 0.08 / math.sqrt(1.0 + step / 100.0)
        for j in range(p):
            reg = 0.0 if j == 0 else ridge * w[j]
            w[j] -= lr * (g[j] / n + reg)
        if step % 100 == 0 and max(abs(v / n) for v in g) < 1e-5:
            break
    return w


def _fit_linear(X: list[list[float]], y: list[float], ridge: float = RIDGE, steps: int = 1200) -> list[float]:
    if not X:
        return []
    p = len(X[0]); w = [0.0] * p
    # Scale target to make gradient behavior stable across MFE/net/holding-time.
    scale = max(1.0, statistics.pstdev(y) if len(y) > 1 else 1.0)
    ys = [v / scale for v in y]
    for step in range(steps):
        g = [0.0] * p
        for xi, yi in zip(X, ys):
            err = _dot(w, xi) - yi
            for j, v in enumerate(xi):
                g[j] += err * v
        n = max(1, len(X))
        lr = 0.04 / math.sqrt(1.0 + step / 100.0)
        for j in range(p):
            reg = 0.0 if j == 0 else ridge * w[j]
            w[j] -= lr * (g[j] / n + reg)
    return [v * scale for v in w]


def _logit(p: float) -> float:
    p = min(1 - 1e-6, max(1e-6, p))
    return math.log(p / (1 - p))


def _fit_platt(raw_probs: list[float], y: list[int]) -> list[float]:
    X = [[1.0, _logit(p)] for p in raw_probs]
    return _fit_logistic(X, y, ridge=0.02, steps=800)


def _calibrate(raw: float, platt: list[float]) -> float:
    if len(platt) != 2:
        return raw
    return _sigmoid(platt[0] + platt[1] * _logit(raw))


def _prob_metrics(probs: list[float], y: list[int]) -> dict:
    if not probs:
        return {'n': 0}
    eps = 1e-9
    brier = sum((p - t) ** 2 for p, t in zip(probs, y)) / len(y)
    logloss = -sum(t * math.log(max(eps, p)) + (1 - t) * math.log(max(eps, 1 - p)) for p, t in zip(probs, y)) / len(y)
    base = sum(y) / len(y)
    base_brier = sum((base - t) ** 2 for t in y) / len(y)
    base_logloss = -(base * math.log(max(eps, base)) + (1 - base) * math.log(max(eps, 1 - base))) if 0 < base < 1 else None
    bins = []
    cal_err_num = 0.0
    for lo in [i / 10 for i in range(10)]:
        ids = [i for i, p in enumerate(probs) if lo <= p < lo + 0.1 or (lo == 0.9 and p == 1.0)]
        if not ids:
            continue
        mp = sum(probs[i] for i in ids) / len(ids)
        ar = sum(y[i] for i in ids) / len(ids)
        cal_err_num += abs(mp - ar) * len(ids)
        bins.append({'lo': lo, 'n': len(ids), 'mean_pred': round(mp, 4), 'actual': round(ar, 4)})
    return {
        'n': len(y), 'positive_rate': round(base, 4), 'brier': round(brier, 6),
        'baseline_brier': round(base_brier, 6), 'brier_skill': round(1 - brier / base_brier, 4) if base_brier > 0 else None,
        'logloss': round(logloss, 6), 'baseline_logloss': None if base_logloss is None else round(base_logloss, 6),
        'calibration_mae': round(cal_err_num / len(y), 4), 'bins': bins,
    }


def _reg_metrics(pred: list[float], y: list[float]) -> dict:
    if not y:
        return {'n': 0}
    mae = sum(abs(a - b) for a, b in zip(pred, y)) / len(y)
    mu = sum(y) / len(y)
    ss_tot = sum((v - mu) ** 2 for v in y)
    ss_res = sum((a - b) ** 2 for a, b in zip(pred, y))
    return {'n': len(y), 'mae': round(mae, 4), 'r2': round(1 - ss_res / ss_tot, 4) if ss_tot > 1e-12 else None}


def _bootstrap_mean_ci(xs: list[float]) -> list[float | None]:
    if len(xs) < 25:
        return [None, None]
    rng = random.Random(99173 + len(xs))
    vals = []
    for _ in range(BOOTSTRAP_N):
        vals.append(sum(xs[rng.randrange(len(xs))] for _ in range(len(xs))) / len(xs))
    vals.sort()
    return [round(vals[int(.025 * (len(vals)-1))], 4), round(vals[int(.975 * (len(vals)-1))], 4)]


def _top_bucket(rows: list[dict], pred_ev: list[float], frac: float = 0.20) -> dict:
    if not rows:
        return {'n': 0}
    k = max(1, int(len(rows) * frac))
    ids = sorted(range(len(rows)), key=lambda i: pred_ev[i], reverse=True)[:k]
    nets = [float(rows[i]['sim_net_pct']) for i in ids]
    return {'n': len(nets), 'mean_net_pct': round(sum(nets) / len(nets), 4), 'median_net_pct': round(statistics.median(nets), 4), 'bootstrap95': _bootstrap_mean_ci(nets)}


def _symbol_holdout(rows: list[dict], preds: list[float]) -> dict:
    ids = []
    for i, r in enumerate(rows):
        sym = str(r.get('symbol') or '')
        bucket = int(hashlib.sha256(sym.encode()).hexdigest()[:8], 16) % 5
        if bucket == 0:
            ids.append(i)
    if len(ids) < 20:
        return {'n': len(ids), 'status': 'INSUFFICIENT'}
    nets = [float(rows[i]['sim_net_pct']) for i in ids]
    top_ids = sorted(ids, key=lambda i: preds[i], reverse=True)[:max(5, len(ids)//5)]
    top = [float(rows[i]['sim_net_pct']) for i in top_ids]
    return {
        'n': len(ids), 'symbols': len(set(str(rows[i].get('symbol')) for i in ids)),
        'mean_net_pct': round(sum(nets)/len(nets), 4),
        'top20_predicted_ev_mean_net_pct': round(sum(top)/len(top), 4),
        'top20_bootstrap95': _bootstrap_mean_ci(top),
    }


def build() -> dict:
    all_rows = _rows()
    # Probability target requires a path-observable TP/SL event label.
    rows = [r for r in all_rows if r.get('tp_before_sl') in {0, 1}]
    now = time.time()
    base = {
        'version': 1, 'feature_version': FEATURE_VERSION, 'generated_at': now,
        'status': 'WAITING_SAMPLE', 'automatic_live_promotion': False,
        'complete_outcomes': len(all_rows), 'path_labeled_outcomes': len(rows), 'minimum_sample': MIN_SAMPLE,
    }
    if len(rows) < MIN_SAMPLE:
        return base

    n = len(rows); n_train = int(n * 0.60); n_cal = int(n * 0.20)
    train, cal, test = rows[:n_train], rows[n_train:n_train+n_cal], rows[n_train+n_cal:]
    y_train = [int(r['tp_before_sl']) for r in train]
    if min(sum(y_train), len(y_train)-sum(y_train)) < 35 or len(test) < MIN_TEST:
        base['status'] = 'WAITING_CLASS_BALANCE_OR_TEST_SAMPLE'
        base['split'] = {'train': len(train), 'calibration': len(cal), 'test': len(test), 'train_positive': sum(y_train)}
        return base

    schema = _fit_schema(train)
    Xtr = [_vec(r, schema) for r in train]; Xcal = [_vec(r, schema) for r in cal]; Xte = [_vec(r, schema) for r in test]
    prob_w = _fit_logistic(Xtr, y_train)
    cal_y = [int(r['tp_before_sl']) for r in cal]
    cal_raw = [_sigmoid(_dot(prob_w, x)) for x in Xcal]
    platt = _fit_platt(cal_raw, cal_y) if len(set(cal_y)) == 2 else [0.0, 1.0]
    test_y = [int(r['tp_before_sl']) for r in test]
    test_prob = [_calibrate(_sigmoid(_dot(prob_w, x)), platt) for x in Xte]

    targets = {
        'net_pct': [float(r['sim_net_pct']) for r in train],
        'mfe_pct': [float(r.get('mfe_pct') or 0.0) for r in train],
        'mae_pct': [float(r.get('mae_pct') or 0.0) for r in train],
        'holding_min': [float(r.get('holding_min') or 60.0) for r in train],
    }
    reg_w = {k: _fit_linear(Xtr, y) for k, y in targets.items()}
    pred = {k: [_dot(reg_w[k], x) for x in Xte] for k in reg_w}
    actual = {
        'net_pct': [float(r['sim_net_pct']) for r in test],
        'mfe_pct': [float(r.get('mfe_pct') or 0.0) for r in test],
        'mae_pct': [float(r.get('mae_pct') or 0.0) for r in test],
        'holding_min': [float(r.get('holding_min') or 60.0) for r in test],
    }
    prob_metrics = _prob_metrics(test_prob, test_y)
    ev_metrics = _reg_metrics(pred['net_pct'], actual['net_pct'])
    top = _top_bucket(test, pred['net_pct'])
    holdout = _symbol_holdout(test, pred['net_pct'])
    regimes = defaultdict(int)
    for r in test:
        regimes[str(r.get('regime') or 'MISSING')] += 1

    # Evidence gate is intentionally demanding and DOES NOT turn on live trading.
    ci_lo = top.get('bootstrap95', [None, None])[0]
    evidence_pass = (
        len(test) >= MIN_TEST
        and (prob_metrics.get('brier_skill') or -1) > 0.02
        and prob_metrics.get('calibration_mae', 1) <= 0.10
        and top.get('n', 0) >= 20
        and ci_lo is not None and ci_lo > 0.0
        and sum(1 for v in regimes.values() if v >= 15) >= 3
    )

    model = {
        'version': 1, 'feature_version': FEATURE_VERSION, 'trained_at': now,
        'schema': schema, 'numeric_features': NUMERIC_FEATURES, 'lanes': LANES, 'regimes': REGIMES,
        'probability_weights': prob_w, 'platt': platt, 'regression_weights': reg_w,
        'training_end_ts': float(train[-1].get('source_ts') or train[-1].get('ts') or 0),
        'calibration_end_ts': float(cal[-1].get('source_ts') or cal[-1].get('ts') or 0),
        'live_enabled': False,
    }
    _atomic(MODEL_PATH, model)

    return {
        **base, 'status': 'EVIDENCE_PASS_SHADOW_ONLY' if evidence_pass else 'EVIDENCE_NOT_YET_PASSING',
        'split': {'train': len(train), 'calibration': len(cal), 'test': len(test)},
        'probability_test': prob_metrics,
        'expected_net_test': ev_metrics,
        'mfe_test': _reg_metrics(pred['mfe_pct'], actual['mfe_pct']),
        'mae_test': _reg_metrics(pred['mae_pct'], actual['mae_pct']),
        'holding_test': _reg_metrics(pred['holding_min'], actual['holding_min']),
        'top20_predicted_ev_test': top,
        'symbol_holdout_test': holdout,
        'test_regime_counts': dict(regimes),
        'evidence_pass': evidence_pass,
        'promotion_policy': {
            'automatic_live_promotion': False,
            'next_required': ['walk-forward', 'separate historical OOS', 'cost stress', 'symbol holdout', 'regime holdout', 'paper/shadow review'],
        },
    }


def run_once() -> dict:
    report = build()
    _atomic(REPORT_PATH, report)
    print(f"[shadow-ev] status={report['status']} complete={report['complete_outcomes']} path={report['path_labeled_outcomes']} evidence={report.get('evidence_pass', False)}", flush=True)
    return report


def main() -> None:
    print(f'[shadow-ev] ONLINE read_only=True min_sample={MIN_SAMPLE} min_test={MIN_TEST} feature_version={FEATURE_VERSION}', flush=True)
    while True:
        started = time.time()
        try:
            run_once()
        except Exception as exc:
            print(f'[shadow-ev] warning {type(exc).__name__}: {str(exc)[:220]}', flush=True)
        time.sleep(max(60.0, POLL_SEC - (time.time() - started)))


if __name__ == '__main__':
    main()
