import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ready_bot'))
import native_paper as bridge


def rich_rows(n=250, interval=3_600_000):
    return [dict(open_time=i * interval, open=99.5, high=101., low=99., close=100.,
                 volume=100., quote_volume=10000., taker_buy_quote=6000.)
            for i in range(n)]


def snapshot():
    return dict(**{'1h_rich': rich_rows(), '15m': rich_rows(250, 900_000)},
                bid=99.99, ask=100., quote_vol=50_000_000)


class NativeBridgeContracts(unittest.TestCase):
    def setUp(self):
        bridge.register_native()

    def test_all_source_files_tracked_and_freqtrade_not_faked(self):
        registry = bridge.source_registry()
        self.assertEqual(len(registry), 10)
        self.assertEqual(sum(x['status'].startswith('PAPER') for x in registry), 5)
        self.assertEqual(sum(x['status'].startswith('BLOCKED') for x in registry), 5)
        self.assertTrue(all((ROOT / x['source']).is_file() for x in registry))
        with self.assertRaisesRegex(RuntimeError, 'Invalid strategy registry|native parity'):
            bridge.validate(dict(mode='paper', strategies=['UNIFIED_CANDIDATE'],
                                 symbols=['BTCUSDT'], starting_cash_usdt=20,
                                 trade_size_usdt=7, max_daily_loss_usdt=2,
                                 max_risk_usdt=.2, max_stop_fraction=.03,
                                 max_open_positions=3, fee_rate=.001, slippage_rate=.0005))

    def test_momentum_uses_native_entry_but_wrapper_exit_is_explicit(self):
        raw = dict(ok=True, strategy='REGIME_ADAPTIVE_RISK_MANAGED_MOMENTUM_V1',
                   liveApproved=False, entry=100., stop=98., notional=6.)
        with patch.object(bridge, 'native_score', return_value=raw) as scorer:
            result = bridge.native_signal(bridge.REGIME, 'ETHUSDT', snapshot(), snapshot())
        self.assertEqual(result['strategy'], bridge.REGIME)
        self.assertEqual(result['target'], 104.)
        self.assertEqual(result['max_stake_usdt'], 6.)
        self.assertEqual(result['hold_interval'], '1h')
        self.assertEqual(result['hold_bars'], 24)
        self.assertAlmostEqual(scorer.call_args.args[1]['takerBuyRatio'], .6)

    def test_smallcap_preserves_original_target_hold_and_notional(self):
        raw = dict(ok=True, strategy=bridge.SMALL_CAP, liveApproved=False,
                   entry=100., stop=99., target=101.5, notional=5.5, maxHoldBars=8)
        with patch.object(bridge, 'native_score', return_value=raw) as scorer:
            result = bridge.native_signal(bridge.SMALL_CAP, 'NEARUSDT', snapshot(), snapshot())
        self.assertEqual(result['target'], 101.5)
        self.assertEqual(result['hold_bars'], 8)
        self.assertEqual(result['hold_interval'], '15m')
        self.assertEqual(result['max_stake_usdt'], 5.5)
        self.assertEqual(scorer.call_args.args[1]['baseAsset'], 'NEAR')

    def test_unconfirmed_native_signal_never_enters(self):
        with patch.object(bridge, 'native_score', return_value=dict(ok=False)):
            self.assertIsNone(bridge.native_signal(bridge.REGIME, 'ETHUSDT', snapshot(), snapshot()))

    def test_missing_native_exit_data_fails_closed(self):
        data = snapshot()
        data.pop('15m')
        with self.assertRaisesRegex(RuntimeError, 'NEEDS_15M'):
            bridge.native_signal(bridge.SMALL_CAP, 'NEARUSDT', data, snapshot())

    def test_no_stake_inflation_to_bypass_minimum(self):
        config = dict(starting_cash_usdt=20., trade_size_usdt=5.5,
                      max_open_positions=3, max_daily_loss_usdt=2.,
                      max_risk_usdt=.2, max_portfolio_risk_usdt=.6,
                      max_stop_fraction=.03, fee_rate=.001, slippage_rate=.0005)
        filters = dict(min_notional=6., max_notional=10000., min_qty=.00001,
                       max_qty=10000., step_size=.00001)
        signal = bridge.core.candidate(bridge.SMALL_CAP, 'NEARUSDT', 100., 99., 102., 1000, 'test')
        state = bridge.core.initial_state(config)
        self.assertEqual(bridge.core.open_position(state, signal, 100., config, filters),
                         'EXCHANGE_NOTIONAL_FILTER')
        self.assertFalse(state['positions'])

    def test_no_exchange_execution_functions_or_credentials(self):
        self.assertFalse(hasattr(bridge, 'place_order'))
        self.assertFalse(hasattr(bridge, 'submit_order'))
        self.assertEqual(bridge.core.initial_state({'starting_cash_usdt': 20})['mode'], 'PAPER_ONLY')


if __name__ == '__main__':
    unittest.main()
