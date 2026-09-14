from __future__ import annotations

import time

import ev_validation_guard
import market_context
import spot_sniper_gate


def _ctx(regime: str, rank: int = 1) -> dict:
    return {
        'context_generated_at': time.time(),
        'regime': regime,
        'opportunity_rank': rank,
        'opportunity_pct_shadow': 90.0,
        'breadth_1h': 0.75,
        'breadth_4h': 0.70,
    }


def _observe_only(_payload: dict) -> dict:
    return {'enforced': False, 'passed': True, 'status': 'OBSERVE_ONLY'}


def test_sideways_low_quality_still_blocked() -> None:
    original_ctx = market_context.symbol_context
    original_ev = spot_sniper_gate.live_ev_gate.evaluate
    try:
        market_context.symbol_context = lambda _symbol: _ctx('SIDEWAYS_COMPRESSION', 1)
        spot_sniper_gate.live_ev_gate.evaluate = _observe_only
        result = spot_sniper_gate.evaluate({
            'symbol': 'DOGEUSDT', 'score': 97,
            'strategy': 'MID_MOMENTUM_CONTINUATION',
            'entry': 1.0, 'target': 1.02, 'stop': 0.99,
        })
    finally:
        market_context.symbol_context = original_ctx
        spot_sniper_gate.live_ev_gate.evaluate = original_ev
    assert result['passed'] is False, result
    assert result['live_authorized'] is False, result
    assert result['status'] == 'REGIME_REJECT', result
    assert result['reason'] == 'sideways-compression-quality-below-exception', result


def test_sideways_score100_mid_can_continue_when_ranked() -> None:
    original_ctx = market_context.symbol_context
    original_ev = spot_sniper_gate.live_ev_gate.evaluate
    try:
        market_context.symbol_context = lambda _symbol: _ctx('SIDEWAYS_COMPRESSION', 5)
        spot_sniper_gate.live_ev_gate.evaluate = _observe_only
        result = spot_sniper_gate.evaluate({
            'symbol': 'ZECUSDT', 'score': 100,
            'strategy': 'MID_MOMENTUM_CONTINUATION',
            'entry': 1.0, 'target': 1.02, 'stop': 0.99,
        })
    finally:
        market_context.symbol_context = original_ctx
        spot_sniper_gate.live_ev_gate.evaluate = original_ev
    assert result['passed'] is True, result
    assert result['live_authorized'] is True, result
    assert result['status'] == 'LIVE_RULES_FALLBACK_PASS', result


def test_observe_only_research_can_use_strict_weak_bull_fallback_when_ranked() -> None:
    original_ctx = market_context.symbol_context
    original_ev = spot_sniper_gate.live_ev_gate.evaluate
    try:
        market_context.symbol_context = lambda _symbol: _ctx('WEAK_BULL', 4)
        spot_sniper_gate.live_ev_gate.evaluate = _observe_only
        result = spot_sniper_gate.evaluate({
            'symbol': 'ZECUSDT', 'score': 100,
            'strategy': 'MID_MOMENTUM_CONTINUATION',
            'entry': 1.0, 'target': 1.02, 'stop': 0.99,
        })
    finally:
        market_context.symbol_context = original_ctx
        spot_sniper_gate.live_ev_gate.evaluate = original_ev
    assert result['passed'] is True, result
    assert result['live_authorized'] is True, result
    assert result['status'] == 'LIVE_RULES_FALLBACK_PASS', result
    assert result['authorization_basis'] == 'PRODUCTION_GATES_RESEARCH_SHADOW', result


def test_score100_can_use_wider_but_bounded_rank() -> None:
    original_ctx = market_context.symbol_context
    original_ev = spot_sniper_gate.live_ev_gate.evaluate
    try:
        market_context.symbol_context = lambda _symbol: _ctx('WEAK_BULL', 12)
        spot_sniper_gate.live_ev_gate.evaluate = _observe_only
        result = spot_sniper_gate.evaluate({
            'symbol': 'PENDLEUSDT', 'score': 100,
            'strategy': 'MID_MOMENTUM_CONTINUATION',
            'entry': 1.0, 'target': 1.02, 'stop': 0.99,
        })
    finally:
        market_context.symbol_context = original_ctx
        spot_sniper_gate.live_ev_gate.evaluate = original_ev
    assert result['passed'] is True, result
    assert result['live_authorized'] is True, result


def test_far_rank_cannot_pass_fallback_on_score_alone() -> None:
    original_ctx = market_context.symbol_context
    original_ev = spot_sniper_gate.live_ev_gate.evaluate
    try:
        market_context.symbol_context = lambda _symbol: _ctx('WEAK_BULL', 31)
        spot_sniper_gate.live_ev_gate.evaluate = _observe_only
        result = spot_sniper_gate.evaluate({
            'symbol': 'DOGEUSDT', 'score': 100,
            'strategy': 'MID_MOMENTUM_CONTINUATION',
            'entry': 1.0, 'target': 1.02, 'stop': 0.99,
        })
    finally:
        market_context.symbol_context = original_ctx
        spot_sniper_gate.live_ev_gate.evaluate = original_ev
    assert result['passed'] is False, result
    assert result['status'] == 'FALLBACK_RANK_REJECT', result


def test_fallback_score_floor_is_hard() -> None:
    original_ctx = market_context.symbol_context
    original_ev = spot_sniper_gate.live_ev_gate.evaluate
    try:
        market_context.symbol_context = lambda _symbol: _ctx('WEAK_BULL', 1)
        spot_sniper_gate.live_ev_gate.evaluate = _observe_only
        result = spot_sniper_gate.evaluate({
            'symbol': 'TRXUSDT', 'score': 91,
            'strategy': 'FAST_PRE_MOMENTUM',
            'entry': 1.0, 'target': 1.012, 'stop': 0.993,
        })
    finally:
        market_context.symbol_context = original_ctx
        spot_sniper_gate.live_ev_gate.evaluate = original_ev
    assert result['passed'] is False, result
    assert result['status'] == 'FALLBACK_SCORE_REJECT', result


def test_weak_bear_has_no_production_fallback() -> None:
    original_ctx = market_context.symbol_context
    original_ev = spot_sniper_gate.live_ev_gate.evaluate
    try:
        market_context.symbol_context = lambda _symbol: _ctx('WEAK_BEAR', 1)
        spot_sniper_gate.live_ev_gate.evaluate = _observe_only
        result = spot_sniper_gate.evaluate({
            'symbol': 'DOGEUSDT', 'score': 100,
            'strategy': 'FAST_PRE_MOMENTUM',
            'entry': 1.0, 'target': 1.012, 'stop': 0.993,
        })
    finally:
        market_context.symbol_context = original_ctx
        spot_sniper_gate.live_ev_gate.evaluate = original_ev
    assert result['passed'] is False, result
    assert result['status'] == 'REGIME_REJECT', result
    assert 'production-fallback-regime-not-allowed' in result['reason'], result


def test_enforced_ev_reject_cannot_be_rescued() -> None:
    original_ctx = market_context.symbol_context
    original_ev = spot_sniper_gate.live_ev_gate.evaluate
    try:
        market_context.symbol_context = lambda _symbol: _ctx('STRONG_BULL', 1)
        spot_sniper_gate.live_ev_gate.evaluate = lambda _payload: {
            'enforced': True, 'passed': False, 'status': 'ENFORCED_REJECT',
            'prob_tp_before_sl': 0.40, 'expected_net_pct': -0.20,
            'expected_mfe_pct': 0.5, 'expected_mae_pct': -0.8,
        }
        result = spot_sniper_gate.evaluate({
            'symbol': 'SOLUSDT', 'score': 100,
            'strategy': 'FAST_PRE_MOMENTUM',
            'entry': 1.0, 'target': 1.012, 'stop': 0.993,
        })
    finally:
        market_context.symbol_context = original_ctx
        spot_sniper_gate.live_ev_gate.evaluate = original_ev
    assert result['passed'] is False, result
    assert result['status'] == 'EV_REJECT', result


def test_approx_historical_evidence_cannot_promote() -> None:
    fake = {
        'status': 'APPROVED', 'evidence_pass': True,
        'runtime_parity': 'APPROXIMATION_ONLY',
        'feature_version': ev_validation_guard.m.FEATURE_VERSION,
        'model': {
            'feature_version': ev_validation_guard.m.FEATURE_VERSION,
            'schema': {'fake': True}, 'probability_weights': [1.0],
            'regression_weights': {'net_pct': [1.0]},
        },
    }
    ok, reason = ev_validation_guard._historical_model_ok(fake)
    assert ok is False, (ok, reason)
    assert reason == 'historical-runtime-parity-not-exact', reason


if __name__ == '__main__':
    test_sideways_low_quality_still_blocked()
    test_sideways_score100_mid_can_continue_when_ranked()
    test_observe_only_research_can_use_strict_weak_bull_fallback_when_ranked()
    test_score100_can_use_wider_but_bounded_rank()
    test_far_rank_cannot_pass_fallback_on_score_alone()
    test_fallback_score_floor_is_hard()
    test_weak_bear_has_no_production_fallback()
    test_enforced_ev_reject_cannot_be_rescued()
    test_approx_historical_evidence_cannot_promote()
    print('[spot-sniper-safety-test] PASS sideways=quality-exception weakbear=blocked score98-rank<=15 far-rank=blocked enforced_ev_reject=hard')
