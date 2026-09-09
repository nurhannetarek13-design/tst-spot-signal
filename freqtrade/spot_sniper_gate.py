from __future__ import annotations

"""Central Spot Sniper decision gate.

Order of operations:
1. market/regime safety,
2. cross-sectional rank,
3. calibrated Probability/EV,
4. existing portfolio/execution hard gates.

The ranking/EV portion only becomes a hard live gate after the forward EV
validation guard approves the current feature version. Panic-regime blocking is
safety-only and is hard immediately.
"""

import os
import time

import live_ev_gate
import market_context

TOP_N = max(1, min(10, int(os.getenv('SPOT_SNIPER_TOP_N', '3'))))
MAX_CONTEXT_AGE_SEC = max(120, int(os.getenv('SPOT_SNIPER_MAX_CONTEXT_AGE_SEC', '600')))
PANIC_BLOCK = (os.getenv('SPOT_SNIPER_BLOCK_PANIC', '1').strip() == '1')
RANK_ONLY_WHEN_EV_APPROVED = (os.getenv('SPOT_SNIPER_RANK_ONLY_WHEN_EV_APPROVED', '1').strip() == '1')


def evaluate(payload: dict) -> dict:
    symbol = str(payload.get('symbol') or '').upper()
    ctx = market_context.symbol_context(symbol)
    generated = float(ctx.get('context_generated_at') or 0.0) if ctx else 0.0
    age = time.time() - generated if generated > 0 else 1e12
    regime = str(ctx.get('regime') or 'UNKNOWN')

    if PANIC_BLOCK and regime == 'PANIC_HIGH_VOL_BEAR':
        return {
            'passed': False, 'status': 'REGIME_REJECT', 'reason': 'panic-high-vol-bear',
            'regime': regime, 'context_age_sec': age,
        }

    ev = live_ev_gate.evaluate(payload)
    ev_enforced = bool(ev.get('enforced'))
    rank = ctx.get('opportunity_rank') if ctx else None
    try:
        rank_i = int(rank) if rank is not None else None
    except Exception:
        rank_i = None

    # Once the calibrated EV model is live-approved, the cross-sectional context
    # must also be current. Before approval it remains telemetry-only so the bot
    # can collect unbiased forward examples.
    if ev_enforced and (not ctx or age > MAX_CONTEXT_AGE_SEC):
        return {
            'passed': False, 'status': 'CONTEXT_REJECT', 'reason': 'market-context-stale-or-missing',
            'regime': regime, 'context_age_sec': age, 'ev': ev,
        }

    rank_enforced = ev_enforced or not RANK_ONLY_WHEN_EV_APPROVED
    if rank_enforced and (rank_i is None or rank_i > TOP_N):
        return {
            'passed': False, 'status': 'RANK_REJECT',
            'reason': f'outside-top-{TOP_N}', 'opportunity_rank': rank_i,
            'regime': regime, 'ev': ev, 'context_age_sec': age,
        }

    if ev_enforced and not bool(ev.get('passed')):
        return {
            'passed': False, 'status': 'EV_REJECT', 'reason': 'calibrated-ev-gate-failed',
            'regime': regime, 'opportunity_rank': rank_i, 'ev': ev,
            'context_age_sec': age,
        }

    return {
        'passed': True,
        'status': 'LIVE_SNIPER_PASS' if ev_enforced else 'WARMUP_OBSERVE',
        'reason': 'validated-ev-and-rank-pass' if ev_enforced else 'collecting-forward-calibration',
        'regime': regime,
        'opportunity_rank': rank_i,
        'opportunity_pct': ctx.get('opportunity_pct_shadow') if ctx else None,
        'breadth_1h': ctx.get('breadth_1h') if ctx else None,
        'breadth_4h': ctx.get('breadth_4h') if ctx else None,
        'context_age_sec': age,
        'ev': ev,
    }
