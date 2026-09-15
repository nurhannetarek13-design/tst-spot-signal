from __future__ import annotations

import market_structure_flow as msf


def base_metrics(**overrides):
    row = {
        'taker_buy_ratio': 0.56,
        'delta_ratio': 0.05,
        'delta_accel': 0.01,
        'depth_imbalance': 0.04,
        'micro_bias_bps': 0.5,
        'profile_extension_atr': 0.0,
        'profile_location': 'ABOVE_POC_IN_VALUE',
        'strong_buy_flow': True,
        'negative_flow': False,
        'absorption': False,
        'phase': 'RANGE',
        'atr_pct': 0.0025,
        'poc': 100.0,
        'val': 99.0,
        'vah': 101.0,
    }
    row.update(overrides)
    return row


def payload(stake=20.0):
    return {'entry': 100.0, 'stop': 99.0, 'target': 102.0, 'stakeUSDT': stake}


def test_good_flow_passes():
    r = msf.decide(base_metrics(), payload(), 'WEAK_BULL')
    assert r['passed'] is True, r
    assert r['quality_score'] >= 60, r


def test_triple_negative_order_flow_blocks():
    r = msf.decide(base_metrics(
        taker_buy_ratio=0.44,
        delta_ratio=-0.14,
        depth_imbalance=-0.18,
        strong_buy_flow=False,
        negative_flow=True,
        profile_location='BELOW_POC_IN_VALUE',
    ), payload(), 'WEAK_BULL')
    assert r['passed'] is False, r
    assert 'triple-negative-order-flow' in r['reason'], r


def test_upthrust_blocks_without_buy_flow():
    r = msf.decide(base_metrics(
        phase='UPTHRUST',
        strong_buy_flow=False,
        taker_buy_ratio=0.51,
        delta_ratio=-0.02,
        depth_imbalance=-0.02,
    ), payload(), 'SIDEWAYS_COMPRESSION')
    assert r['passed'] is False, r
    assert 'wyckoff-upthrust-trap' in r['reason'], r


def test_profile_chase_blocks_without_flow():
    r = msf.decide(base_metrics(
        profile_location='ABOVE_VAH',
        profile_extension_atr=2.2,
        strong_buy_flow=False,
        taker_buy_ratio=0.52,
        delta_ratio=0.0,
        depth_imbalance=0.0,
    ), payload(), 'WEAK_BULL')
    assert r['passed'] is False, r
    assert 'volume-profile-chase' in r['reason'], r


def test_quality_sizing_never_increases_stake():
    r = msf.decide(base_metrics(
        taker_buy_ratio=0.52,
        delta_ratio=0.01,
        depth_imbalance=0.01,
        strong_buy_flow=False,
        profile_location='BELOW_POC_IN_VALUE',
    ), payload(30.0), 'WEAK_BULL')
    assert r['adjusted_stake_usdt'] <= 30.0, r


def test_risk_cap_reduces_large_risk_stake():
    p = {'entry': 100.0, 'stop': 98.0, 'target': 103.0, 'stakeUSDT': 40.0}
    r = msf.decide(base_metrics(), p, 'WEAK_BULL')
    assert r['adjusted_stake_usdt'] <= 25.0 + 1e-9, r


if __name__ == '__main__':
    test_good_flow_passes()
    test_triple_negative_order_flow_blocks()
    test_upthrust_blocks_without_buy_flow()
    test_profile_chase_blocks_without_flow()
    test_quality_sizing_never_increases_stake()
    test_risk_cap_reduces_large_risk_stake()
    print('[market-structure-flow-test] PASS positive-flow negative-flow upthrust profile-chase risk-sizing')
