from __future__ import annotations

"""Central Spot Sniper decision gate.

Live execution is fail-closed. A heuristic score alone can never authorize a
real order. Research/warmup/observe states remain useful for telemetry, but they
must never be treated as live permission.

Order of operations:
1. market/regime safety,
2. validated Probability/EV approval,
3. fresh cross-sectional context + rank,
4. mandatory finite EV evidence,
5. existing portfolio/execution hard gates in the caller.
"""

import math
import os
import time

import live_ev_gate
import market_context

TOP_N = max(1, min(10, int(os.getenv('SPOT_SNIPER_TOP_N', '3'))))
MAX_CONTEXT_AGE_SEC = max(120, int(os.getenv('SPOT_SNIPER_MAX_CONTEXT_AGE_SEC', '600')))
PANIC_BLOCK = (os.getenv('SPOT_SNIPER_BLOCK_PANIC', '1').strip() == '1')

# These are the minimum model outputs required before a real BUY can ever pass.
REQUIRED_EV_FIELDS = (
    'prob_tp_before_sl',
    'expected_net_pct',
    'expected_mfe_pct',
    'expected_mae_pct',
)


def _finite_number(value) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _reject(status: str, reason: str, *, regime: str, age: float, rank=None, ev=None) -> dict:
    return {
        'passed': False,
        'live_authorized': False,
        'status': status,
        'reason': reason,
        'regime': regime,
        'opportunity_rank': rank,
        'context_age_sec': age,
        'ev': ev or {},
    }


def evaluate(payload: dict) -> dict:
    symbol = str(payload.get('symbol') or '').upper()
    ctx = market_context.symbol_context(symbol)
    generated = float(ctx.get('context_generated_at') or 0.0) if ctx else 0.0
    age = time.time() - generated if generated > 0 else 1e12
    regime = str(ctx.get('regime') or 'UNKNOWN')

    if PANIC_BLOCK and regime == 'PANIC_HIGH_VOL_BEAR':
        return _reject(
            'REGIME_REJECT', 'panic-high-vol-bear',
            regime=regime, age=age,
        )

    ev = live_ev_gate.evaluate(payload)
    ev_enforced = bool(ev.get('enforced'))
    ev_status = str(ev.get('status') or 'UNKNOWN')

    rank = ctx.get('opportunity_rank') if ctx else None
    try:
        rank_i = int(rank) if rank is not None else None
    except Exception:
        rank_i = None

    # Critical fail-closed rule: OFF / OBSERVE / WARMUP / missing model approval
    # are telemetry states only. They can NEVER become a real order, regardless
    # of Score=100 or any downstream one-tap/execution setting.
    if not ev_enforced:
        return _reject(
            'WARMUP_BLOCK', f'ev-not-live-enforced:{ev_status}',
            regime=regime, age=age, rank=rank_i, ev=ev,
        )

    # Even in explicit enforce mode, the model must return an actual passing
    # enforced decision. This blocks missing models, failed inference and stale
    # approvals from accidentally falling through.
    if ev_status != 'ENFORCED_PASS' or not bool(ev.get('passed')):
        return _reject(
            'EV_REJECT', f'calibrated-ev-gate-not-pass:{ev_status}',
            regime=regime, age=age, rank=rank_i, ev=ev,
        )

    # Live-approved EV is not enough if the market context is missing/stale.
    if not ctx or age > MAX_CONTEXT_AGE_SEC:
        return _reject(
            'CONTEXT_REJECT', 'market-context-stale-or-missing',
            regime=regime, age=age, rank=rank_i, ev=ev,
        )

    # Rank is mandatory for live trading. None is a rejection, never a bypass.
    if rank_i is None or rank_i > TOP_N:
        return _reject(
            'RANK_REJECT', f'outside-top-{TOP_N}',
            regime=regime, age=age, rank=rank_i, ev=ev,
        )

    # Missing/NaN/inf EV evidence is also a hard rejection.
    missing = [name for name in REQUIRED_EV_FIELDS if not _finite_number(ev.get(name))]
    if missing:
        return _reject(
            'EVIDENCE_REJECT', 'mandatory-live-evidence-missing:' + ','.join(missing),
            regime=regime, age=age, rank=rank_i, ev=ev,
        )

    probability = float(ev['prob_tp_before_sl'])
    if probability < 0.0 or probability > 1.0:
        return _reject(
            'EVIDENCE_REJECT', 'probability-out-of-range',
            regime=regime, age=age, rank=rank_i, ev=ev,
        )

    return {
        'passed': True,
        'live_authorized': True,
        'status': 'LIVE_SNIPER_PASS',
        'reason': 'validated-ev-rank-context-evidence-pass',
        'regime': regime,
        'opportunity_rank': rank_i,
        'opportunity_pct': ctx.get('opportunity_pct_shadow'),
        'breadth_1h': ctx.get('breadth_1h'),
        'breadth_4h': ctx.get('breadth_4h'),
        'context_age_sec': age,
        'ev': ev,
    }
