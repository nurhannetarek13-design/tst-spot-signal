from __future__ import annotations

import time

import dynamic_exit_manager as dex
import fast_entry_engine as fast


def test_noise_aware_stop_floor() -> None:
    stop = fast._whipsaw_stop_pct(0.0055, {'atr_pct': 0.0020})
    assert 0.0075 <= stop <= 0.0110, stop


def test_pre_entry_high_does_not_fake_profit() -> None:
    opened = time.time()
    pos = {
        'entry': 100.0,
        'stop': 99.20,
        'target': 102.0,
        'peak_price': 100.0,
        'opened_at': opened,
        'initial_risk_per_unit': 0.80,
    }
    market = {
        'bid': 100.05, 'ask': 100.06, 'high20': 105.0,
        'swing_low7': 99.8, 'atr': 0.20, 'atr_pct': 0.0020,
        'last_open': 104.0, 'last_high': 105.0, 'last_low': 99.9, 'last_close': 100.0,
        'last_open_time': opened - 60.0, 'last_close_time': opened - 1.0, 'last_red': True,
    }
    suggested, reason = dex._suggest_stop(pos, market)
    assert suggested is None, (suggested, reason)


def test_red_reversal_locks_earned_profit() -> None:
    opened = time.time() - 600.0
    pos = {
        'entry': 100.0,
        'stop': 99.20,
        'target': 102.5,
        'peak_price': 101.20,
        'opened_at': opened,
        'initial_risk_per_unit': 0.80,
    }
    market = {
        'bid': 100.82, 'ask': 100.84, 'high20': 101.20,
        'swing_low7': 100.40, 'atr': 0.20, 'atr_pct': 0.0020,
        'last_open': 101.05, 'last_high': 101.20, 'last_low': 100.75, 'last_close': 100.80,
        'last_open_time': opened + 540.0, 'last_close_time': opened + 599.0, 'last_red': True,
    }
    suggested, reason = dex._suggest_stop(pos, market)
    assert suggested is not None, reason
    assert suggested > 100.0, (suggested, reason)
    assert reason in {
        'red-reversal-profit-lock', 'profit-giveback-lock',
        'structure-lock', 'near-target-lock', 'breakeven-plus',
    }, reason


if __name__ == '__main__':
    test_noise_aware_stop_floor()
    test_pre_entry_high_does_not_fake_profit()
    test_red_reversal_locks_earned_profit()
    print('[test-early-whipsaw-protection] PASS')
