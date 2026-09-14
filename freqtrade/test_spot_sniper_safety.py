from __future__ import annotations

import time

import ev_validation_guard
import market_context
import spot_sniper_gate


def _ctx(regime: str, rank=1, n: int = 80) -> dict:
    return {
        'context_generated_at': time.time(),
        'regime': regime,
        'opportunity_rank': rank,
        'opportunity_pct_shadow': 90.0,
        'breadth_1h': 0.75,
        'breadth_4h': 0.70,
        'universe_n': n,
    }


def _observe_only(_payload: dict) -> dict:
    return {'enforced': False, 'passed': True, 'status': 'OBSERVE_ONLY'}


def _eval(ctx: dict, payload: dict) -> dict:
    original_ctx = market_context.symbol_context
    original_ev = spot_sniper_gate.live_ev_gate.evaluate
    try:
        market_context.symbol_context = lambda _symbol: ctx
        spot_sniper_gate.live_ev_gate.evaluate = _observe_only
        return spot_sniper_gate.evaluate(payload)
    finally:
        market_context.symbol_context = original_ctx
        spot_sniper_gate.live_ev_gate.evaluate = original_ev


def test_sideways_low_quality_still_blocked() -> None:
    result = _eval(_ctx('SIDEWAYS_COMPRESSION', 1), {
        'symbol': 'DOGEUSDT', 'score': 97,
        'strategy': 'MID_MOMENTUM_CONTINUATION',
        'entry': 1.0, 'target': 1.02, 'stop': 0.99,
    })
    assert result['passed'] is False, result
    assert result['status'] == 'REGIME_REJECT', result
    assert result['reason'] == 'sideways-compression-quality-below-exception', result


def test_sideways_score100_mid_can_continue_when_ranked() -> None:
    result = _eval(_ctx('SIDEWAYS_COMPRESSION', 5), {
        'symbol': 'ZECUSDT', 'score': 100,
        'strategy': 'MID_MOMENTUM_CONTINUATION',
        'entry': 1.0, 'target': 1.02, 'stop': 0.99,
    })
    assert result['passed'] is True, result
    assert result['live_authorized'] is True, result
    assert result['status'] == 'LIVE_RULES_FALLBACK_PASS', result


def test_observe_only_research_can_use_strict_weak_bull_fallback_when_ranked() -> None:
    result = _eval(_ctx('WEAK_BULL', 4), {
        'symbol': 'ZECUSDT', 'score': 100,
        'strategy': 'MID_MOMENTUM_CONTINUATION',
        'entry': 1.0, 'target': 1.02, 'stop': 0.99,
    })
    assert result['passed'] is True, result
    assert result['authorization_basis'] == 'PRODUCTION_GATES_RESEARCH_SHADOW', result


def test_score100_can_use_wider_but_bounded_rank() -> None:
    result = _eval(_ctx('WEAK_BULL', 12), {
        'symbol': 'PENDLEUSDT', 'score': 100,
        'strategy': 'MID_MOMENTUM_CONTINUATION',
        'entry': 1.0, 'target': 1.02, 'stop': 0.99,
    })
    assert result['passed'] is True, result


def test_far_rank_cannot_pass_fallback_on_score_alone_when_context_reliable() -> None:
    result = _eval(_ctx('WEAK_BULL', 31, 80), {
        'symbol': 'DOGEUSDT', 'score': 100,
        'strategy': 'MID_MOMENTUM_CONTINUATION',
        'entry': 1.0, 'target': 1.02, 'stop': 0.99,
    })
    assert result['passed'] is False, result
    assert result['status'] == 'FALLBACK_RANK_REJECT', result


def test_fallback_score_floor_is_hard() -> None:
    result = _eval(_ctx('WEAK_BULL', 1), {
        'symbol': 'TRXUSDT', 'score': 91,
        'strategy': 'FAST_PRE_MOMENTUM',
        'entry': 1.0, 'target': 1.012, 'stop': 0.993,
    })
    assert result['passed'] is False, result
    assert result['status'] == 'FALLBACK_SCORE_REJECT', result


def test_weak_bear_normal_lane_stays_blocked() -> None:
    result = _eval(_ctx('WEAK_BEAR', 1), {
        'symbol': 'DOGEUSDT', 'score': 100,
        'strategy': 'FAST_PRE_MOMENTUM',
        'entry': 1.0, 'target': 1.012, 'stop': 0.993,
    })
    assert result['passed'] is False, result
    assert result['status'] == 'REGIME_REJECT', result


def test_weak_bear_mid_needs_exceptional_score() -> None:
    result = _eval(_ctx('WEAK_BEAR', 1), {
        'symbol': 'CAKEUSDT', 'score': 96,
        'strategy': 'MID_MOMENTUM_CONTINUATION',
        'entry': 1.0, 'target': 1.02, 'stop': 0.99,
    })
    assert result['passed'] is False, result
    assert result['status'] == 'FALLBACK_SCORE_REJECT', result


def test_weak_bear_score100_mid_can_continue_when_context_is_thin_and_unranked() -> None:
    result = _eval(_ctx('WEAK_BEAR', None, 22), {
        'symbol': 'INJUSDT', 'score': 100,
        'strategy': 'MID_MOMENTUM_CONTINUATION',
        'entry': 1.0, 'target': 1.02, 'stop': 0.99,
    })
    assert result['passed'] is True, result
    assert result['live_authorized'] is True, result


def test_weak_bear_reversal_score98_can_continue_when_thin_context_rank_is_noisy() -> None:
    result = _eval(_ctx('WEAK_BEAR', 20, 22), {
        'symbol': 'REVUSDT', 'score': 98,
        'strategy': 'EARLY_REVERSAL_STARTER',
        'entry': 1.0, 'target': 1.02, 'stop': 0.99,
    })
    assert result['passed'] is True, result
    assert result['live_authorized'] is True, result


def test_weak_bear_explosive_still_blocked() -> None:
    result = _eval(_ctx('WEAK_BEAR', 1), {
        'symbol': 'PUMPUSDT', 'score': 100,
        'strategy': 'EXPLOSIVE_CONTINUATION',
        'entry': 1.0, 'target': 1.03, 'stop': 0.99,
    })
    assert result['passed'] is False, result
    assert result['status'] == 'REGIME_REJECT', result


def test_panic_remains_hard_block() -> None:
    result = _eval(_ctx('PANIC_HIGH_VOL_BEAR', 1), {
        'symbol': 'SOLUSDT', 'score': 100,
        'strategy': 'MID_MOMENTUM_CONTINUATION',
        'entry': 1.0, 'target': 1.02, 'stop': 0.99,
    })
    assert result['passed'] is False, result
    assert result['status'] == 'REGIME_REJECT', result
    assert result['reason'] == 'panic-high-vol-bear', result


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
    test_far_rank_cannot_pass_fallback_on_score_alone_when_context_reliable()
    test_fallback_score_floor_is_hard()
    test_weak_bear_normal_lane_stays_blocked()
    test_weak_bear_mid_needs_exceptional_score()
    test_weak_bear_score100_mid_can_continue_when_context_is_thin_and_unranked()
    test_weak_bear_reversal_score98_can_continue_when_thin_context_rank_is_noisy()
    test_weak_bear_explosive_still_blocked()
    test_panic_remains_hard_block()
    test_enforced_ev_reject_cannot_be_rescued()
    test_approx_historical_evidence_cannot_promote()
    print('[spot-sniper-safety-test] PASS sideways=selective weakbear=MID97+REV98 only thin-context=score100-unranked panic=hard reliable-rank=bounded enforced-ev-reject=hard')
