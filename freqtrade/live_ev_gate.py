from __future__ import annotations

"""Runtime Probability/EV decision layer.

This module never invents a probability from the heuristic Score/100. It will
only enforce a probability/EV decision when a separately validated forward
model has been approved by ev_validation_guard.py. Until then it runs in WARMUP
mode and returns transparent "not calibrated yet" diagnostics without pretending
that Score is a probability.
"""

import json
import math
import os
import time
from pathlib import Path

import market_context
import shadow_ev_model

MODEL_PATH = Path(os.getenv('TST_SHADOW_EV_MODEL_PATH', '/data/tst_shadow_ev_model.json'))
REPORT_PATH = Path(os.getenv('TST_SHADOW_EV_REPORT_PATH', '/data/tst_shadow_ev_report.json'))
APPROVAL_PATH = Path(os.getenv('TST_EV_LIVE_APPROVAL_PATH', '/data/tst_ev_live_approval.json'))
MODE = (os.getenv('SPOT_SNIPER_EV_MODE') or 'auto').strip().lower()  # off|observe|auto|enforce
MIN_PROB = float(os.getenv('SPOT_SNIPER_MIN_TP_PROB', '0.56'))
MIN_NET_EV_PCT = float(os.getenv('SPOT_SNIPER_MIN_NET_EV_PCT', '0.12'))
MIN_MFE_MAE = float(os.getenv('SPOT_SNIPER_MIN_MFE_MAE', '1.35'))
MAX_MODEL_AGE_SEC = int(os.getenv('SPOT_SNIPER_MAX_MODEL_AGE_SEC', str(36 * 3600)))


def _read(path: Path) -> dict:
    try:
        row = json.loads(path.read_text(encoding='utf-8'))
        return row if isinstance(row, dict) else {}
    except Exception:
        return {}


def _lane(payload: dict) -> str:
    s = str(payload.get('strategy') or '').upper()
    if s.startswith('MID_MOMENTUM_CONTINUATION'):
        return 'MID'
    if s.startswith('EXPLOSIVE_CONTINUATION'):
        return 'EXPLOSIVE'
    if s.startswith('EXTREME_CONTINUATION'):
        return 'EXTREME'
    return 'NORMAL'


def _row(payload: dict) -> dict:
    symbol = str(payload.get('symbol') or '').upper()
    ctx = market_context.symbol_context(symbol)
    try:
        entry = float(payload.get('entry') or 0.0)
        stop = float(payload.get('stop') or 0.0)
        target = float(payload.get('target') or 0.0)
    except Exception:
        entry = stop = target = 0.0
    row = {
        'symbol': symbol,
        'lane': _lane(payload),
        'score': payload.get('score'),
        'risk_pct': ((entry - stop) / entry) if entry > 0 and stop > 0 else None,
        'reward_pct': ((target - entry) / entry) if entry > 0 and target > 0 else None,
        **ctx,
    }
    return row


def _approval_ok(model: dict, report: dict, approval: dict) -> tuple[bool, str]:
    now = time.time()
    fv = str(model.get('feature_version') or '')
    if not fv or fv != str(shadow_ev_model.FEATURE_VERSION):
        return False, 'feature-version-mismatch'
    if str(approval.get('status') or '') != 'APPROVED':
        return False, 'forward-validation-not-approved'
    if str(approval.get('feature_version') or '') != fv:
        return False, 'approval-feature-version-mismatch'
    if not bool(report.get('evidence_pass')):
        return False, 'model-evidence-not-passing'
    trained = float(model.get('trained_at') or 0.0)
    if trained <= 0 or now - trained > MAX_MODEL_AGE_SEC:
        return False, 'model-stale'
    approved_at = float(approval.get('approved_at') or 0.0)
    if approved_at <= 0 or now - approved_at > MAX_MODEL_AGE_SEC:
        return False, 'approval-stale'
    return True, 'approved'


def _predict(model: dict, payload: dict) -> dict:
    row = _row(payload)
    schema = model.get('schema') or {}
    x = shadow_ev_model._vec(row, schema)
    pw = model.get('probability_weights') or []
    platt = model.get('platt') or []
    raw = shadow_ev_model._sigmoid(shadow_ev_model._dot(pw, x))
    prob = shadow_ev_model._calibrate(raw, platt)
    rw = model.get('regression_weights') or {}

    def pred(name: str):
        w = rw.get(name) or []
        return shadow_ev_model._dot(w, x) if w else None

    return {
        'prob_tp_before_sl': float(prob),
        'expected_net_pct': pred('net_pct'),
        'expected_mfe_pct': pred('mfe_pct'),
        'expected_mae_pct': pred('mae_pct'),
        'expected_holding_min': pred('holding_min'),
        'regime': row.get('regime'),
        'opportunity_rank': row.get('opportunity_rank'),
        'opportunity_pct': row.get('opportunity_pct_shadow'),
        'feature_version': model.get('feature_version'),
    }


def evaluate(payload: dict) -> dict:
    """Return a transparent EV decision.

    In auto mode, enforcement only switches on after the forward validation
    approval file is current and matches the model feature version.
    """
    mode = MODE if MODE in {'off', 'observe', 'auto', 'enforce'} else 'auto'
    if mode == 'off':
        return {'enforced': False, 'passed': True, 'status': 'OFF'}

    model = _read(MODEL_PATH)
    report = _read(REPORT_PATH)
    approval = _read(APPROVAL_PATH)
    ok, why = _approval_ok(model, report, approval) if model else (False, 'model-unavailable')

    if not model or not model.get('schema'):
        return {
            'enforced': mode == 'enforce',
            'passed': mode != 'enforce',
            'status': 'WARMUP_NO_MODEL',
            'reason': why,
        }

    try:
        pred = _predict(model, payload)
    except Exception as exc:
        return {
            'enforced': mode == 'enforce',
            'passed': mode != 'enforce',
            'status': 'MODEL_INFERENCE_FAILED',
            'reason': f'{type(exc).__name__}:{str(exc)[:120]}',
        }

    enforced = (mode == 'enforce') or (mode == 'auto' and ok)
    p = float(pred.get('prob_tp_before_sl') or 0.0)
    ev = pred.get('expected_net_pct')
    mfe = pred.get('expected_mfe_pct')
    mae = pred.get('expected_mae_pct')
    try:
        ratio = float(mfe) / max(abs(float(mae)), 1e-9)
    except Exception:
        ratio = 0.0

    criteria = {
        'probability': p >= MIN_PROB,
        'net_ev': ev is not None and float(ev) >= MIN_NET_EV_PCT,
        'mfe_mae': ratio >= MIN_MFE_MAE,
    }
    pass_model = all(criteria.values())
    status = 'ENFORCED_PASS' if enforced and pass_model else ('ENFORCED_REJECT' if enforced else 'OBSERVE_ONLY')
    return {
        **pred,
        'enforced': enforced,
        'passed': pass_model if enforced else True,
        'status': status,
        'validation_status': why,
        'criteria': criteria,
        'thresholds': {
            'min_prob_tp_before_sl': MIN_PROB,
            'min_expected_net_pct': MIN_NET_EV_PCT,
            'min_expected_mfe_mae': MIN_MFE_MAE,
        },
    }
