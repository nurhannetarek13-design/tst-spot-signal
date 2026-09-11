from __future__ import annotations

"""Central Spot Sniper decision gate.

There are deliberately two live decision paths:

1. VALIDATED EV PATH
   If Probability/EV research is explicitly approved and live-enforced, it keeps
   full authority. An enforced EV rejection can never be rescued by heuristics.

2. PRODUCTION FALLBACK PATH
   While research is OBSERVE/WARMUP/RESEARCH_ONLY, it remains telemetry only and
   is not allowed to create a permanent live-trading deadlock. A candidate may
   proceed only in a constructive market regime, after the existing upstream
   quality/BTC/HTF/microstructure gates, with a regime- and lane-adjusted score.

Research isolation rules:
- Gate A remains frozen/research-only and is not changed here.
- Tardis/L2/Gate G remain research-only and have zero live influence.
- Unapproved historical/forward EV evidence neither authorizes nor vetoes live.
- SIDEWAYS_COMPRESSION and PANIC remain hard blocks.
- Existing portfolio, loss, execution, OCO and user-confirmation gates remain
  downstream hard gates and cannot be bypassed here.
"""

import math
import os
import time

import live_ev_gate
import market_context

TOP_N = max(1, min(10, int(os.getenv('SPOT_SNIPER_TOP_N', '3'))))
MAX_CONTEXT_AGE_SEC = max(120, int(os.getenv('SPOT_SNIPER_MAX_CONTEXT_AGE_SEC', '600')))
PANIC_BLOCK = (os.getenv('SPOT_SNIPER_BLOCK_PANIC', '1').strip() == '1')
SIDEWAYS_BLOCK = (os.getenv('SPOT_SNIPER_BLOCK_SIDEWAYS', '1').strip() == '1')
RESEARCH_FALLBACK_ENABLED = (os.getenv('SPOT_SNIPER_RESEARCH_FALLBACK', '1').strip() == '1')

# Conservative production fallback. These are heuristic execution thresholds,
# NOT win probabilities and NOT research promotion claims.
FALLBACK_STRONG_BULL_SCORE = float(os.getenv('SPOT_SNIPER_FALLBACK_STRONG_BULL_SCORE', '90'))
FALLBACK_WEAK_BULL_SCORE = float(os.getenv('SPOT_SNIPER_FALLBACK_WEAK_BULL_SCORE', '92'))
FALLBACK_POST_CRASH_SCORE = float(os.getenv('SPOT_SNIPER_FALLBACK_POST_CRASH_SCORE', '95'))
FALLBACK_MID_PENALTY = float(os.getenv('SPOT_SNIPER_FALLBACK_MID_PENALTY', '2'))
FALLBACK_EXPLOSIVE_PENALTY = float(os.getenv('SPOT_SNIPER_FALLBACK_EXPLOSIVE_PENALTY', '3'))
FALLBACK_EXTREME_PENALTY = float(os.getenv('SPOT_SNIPER_FALLBACK_EXTREME_PENALTY', '5'))

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


def _lane(payload: dict) -> str:
    strategy = str(payload.get('strategy') or '').upper()
    if strategy.startswith('MID_MOMENTUM_CONTINUATION'):
        return 'MID'
    if strategy.startswith('EXPLOSIVE_CONTINUATION'):
        return 'EXPLOSIVE'
    if strategy.startswith('EXTREME_CONTINUATION'):
        return 'EXTREME'
    return 'NORMAL'


def _fallback_threshold(regime: str, lane: str) -> float | None:
    if regime == 'STRONG_BULL':
        base = FALLBACK_STRONG_BULL_SCORE
    elif regime == 'WEAK_BULL':
        base = FALLBACK_WEAK_BULL_SCORE
    elif regime == 'POST_CRASH_RECOVERY':
        base = FALLBACK_POST_CRASH_SCORE
    else:
        return None

    penalty = {
        'NORMAL': 0.0,
        'MID': FALLBACK_MID_PENALTY,
        'EXPLOSIVE': FALLBACK_EXPLOSIVE_PENALTY,
        'EXTREME': FALLBACK_EXTREME_PENALTY,
    }.get(lane, FALLBACK_EXTREME_PENALTY)
    return min(100.0, base + penalty)


def _reject(status: str, reason: str, *, regime: str, age: float, rank=None, ev=None, **extra) -> dict:
    return {
        'passed': False,
        'live_authorized': False,
        'status': status,
        'reason': reason,
        'regime': regime,
        'opportunity_rank': rank,
        'context_age_sec': age,
        'ev': ev or {},
        **extra,
    }


def evaluate(payload: dict) -> dict:
    symbol = str(payload.get('symbol') or '').upper()
    ctx = market_context.symbol_context(symbol)
    generated = float(ctx.get('context_generated_at') or 0.0) if ctx else 0.0
    age = time.time() - generated if generated > 0 else 1e12
    regime = str(ctx.get('regime') or 'UNKNOWN')
    lane = _lane(payload)
    try:
        score = float(payload.get('score') or 0.0)
    except Exception:
        score = 0.0

    if PANIC_BLOCK and regime == 'PANIC_HIGH_VOL_BEAR':
        return _reject('REGIME_REJECT', 'panic-high-vol-bear', regime=regime, age=age, lane=lane)

    if SIDEWAYS_BLOCK and regime == 'SIDEWAYS_COMPRESSION':
        return _reject(
            'REGIME_REJECT', 'sideways-compression-no-validated-edge',
            regime=regime, age=age, lane=lane,
        )

    # Fresh context is required for either path. A missing/stale research context
    # cannot accidentally become permission just because the EV model is warming.
    if not ctx or age > MAX_CONTEXT_AGE_SEC:
        return _reject(
            'CONTEXT_REJECT', 'market-context-stale-or-missing',
            regime=regime, age=age, lane=lane,
        )

    rank = ctx.get('opportunity_rank')
    try:
        rank_i = int(rank) if rank is not None else None
    except Exception:
        rank_i = None

    ev = live_ev_gate.evaluate(payload)
    ev_enforced = bool(ev.get('enforced'))
    ev_status = str(ev.get('status') or 'UNKNOWN')

    # ------------------------------------------------------------------
    # Path 1: approved/live-enforced Probability/EV model.
    # Once enforced, its reject is final: production fallback cannot override it.
    # ------------------------------------------------------------------
    if ev_enforced:
        if ev_status != 'ENFORCED_PASS' or not bool(ev.get('passed')):
            return _reject(
                'EV_REJECT', f'calibrated-ev-gate-not-pass:{ev_status}',
                regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
            )

        if rank_i is None or rank_i > TOP_N:
            return _reject(
                'RANK_REJECT', f'outside-top-{TOP_N}',
                regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
            )

        missing = [name for name in REQUIRED_EV_FIELDS if not _finite_number(ev.get(name))]
        if missing:
            return _reject(
                'EVIDENCE_REJECT', 'mandatory-live-evidence-missing:' + ','.join(missing),
                regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
            )

        probability = float(ev['prob_tp_before_sl'])
        if probability < 0.0 or probability > 1.0:
            return _reject(
                'EVIDENCE_REJECT', 'probability-out-of-range',
                regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
            )

        return {
            'passed': True,
            'live_authorized': True,
            'status': 'LIVE_SNIPER_PASS',
            'reason': 'validated-ev-rank-context-evidence-pass',
            'authorization_basis': 'VALIDATED_EV',
            'regime': regime,
            'lane': lane,
            'opportunity_rank': rank_i,
            'opportunity_pct': ctx.get('opportunity_pct_shadow'),
            'breadth_1h': ctx.get('breadth_1h'),
            'breadth_4h': ctx.get('breadth_4h'),
            'context_age_sec': age,
            'ev': ev,
        }

    # ------------------------------------------------------------------
    # Path 2: research is not promoted. Keep it shadow-only rather than letting
    # OBSERVE/WARMUP become an accidental permanent kill-switch for production.
    # This path does NOT claim statistical validation.
    # ------------------------------------------------------------------
    if not RESEARCH_FALLBACK_ENABLED:
        return _reject(
            'WARMUP_BLOCK', f'ev-not-live-enforced:{ev_status}',
            regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
        )

    required_score = _fallback_threshold(regime, lane)
    if required_score is None:
        return _reject(
            'REGIME_REJECT', f'production-fallback-regime-not-allowed:{regime}',
            regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
        )

    if score < required_score:
        return _reject(
            'FALLBACK_SCORE_REJECT', f'production-fallback-score<{required_score:.0f}',
            regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
            fallback_score_required=required_score,
        )

    return {
        'passed': True,
        'live_authorized': True,
        'status': 'LIVE_RULES_FALLBACK_PASS',
        'reason': 'research-shadow-production-gates-pass',
        'authorization_basis': 'PRODUCTION_GATES_RESEARCH_SHADOW',
        'regime': regime,
        'lane': lane,
        'score': score,
        'fallback_score_required': required_score,
        'opportunity_rank': rank_i,
        'opportunity_pct': ctx.get('opportunity_pct_shadow'),
        'breadth_1h': ctx.get('breadth_1h'),
        'breadth_4h': ctx.get('breadth_4h'),
        'context_age_sec': age,
        'ev': ev,
    }
