from __future__ import annotations

import time

import ev_validation_guard
import market_context
import spot_sniper_gate


def test_sideways_is_fail_closed() -> None:
    original = market_context.symbol_context
    try:
        market_context.symbol_context = lambda _symbol: {
            'context_generated_at': time.time(),
            'regime': 'SIDEWAYS_COMPRESSION',
            'opportunity_rank': 1,
            'opportunity_pct_shadow': 99.9,
            'breadth_1h': 0.50,
            'breadth_4h': 0.50,
        }
        result = spot_sniper_gate.evaluate({
            'symbol': 'DOGEUSDT',
            'score': 100,
            'entry': 1.0,
            'target': 1.02,
            'stop': 0.99,
        })
    finally:
        market_context.symbol_context = original

    assert result['passed'] is False, result
    assert result['live_authorized'] is False, result
    assert result['status'] == 'REGIME_REJECT', result
    assert result['reason'] == 'sideways-compression-no-validated-edge', result


def test_approx_historical_evidence_cannot_promote() -> None:
    fake = {
        'status': 'APPROVED',
        'evidence_pass': True,
        'runtime_parity': 'APPROXIMATION_ONLY',
        'feature_version': ev_validation_guard.m.FEATURE_VERSION,
        'model': {
            'feature_version': ev_validation_guard.m.FEATURE_VERSION,
            'schema': {'fake': True},
            'probability_weights': [1.0],
            'regression_weights': {'net_pct': [1.0]},
        },
    }
    ok, reason = ev_validation_guard._historical_model_ok(fake)
    assert ok is False, (ok, reason)
    assert reason == 'historical-runtime-parity-not-exact', reason


if __name__ == '__main__':
    test_sideways_is_fail_closed()
    test_approx_historical_evidence_cannot_promote()
    print('[spot-sniper-safety-test] PASS sideways_score100=blocked historical_approx=blocked')
