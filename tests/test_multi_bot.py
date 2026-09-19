import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

MOD_PATH = Path(__file__).resolve().parents[1] / 'ready_bot' / 'multi_bot.py'
spec = importlib.util.spec_from_file_location('multi_bot', MOD_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules['multi_bot'] = mod
spec.loader.exec_module(mod)

CFG = {'starting_cash_usdt': 20.0, 'trade_size_usdt': 5.0,
       'max_open_positions': 3, 'max_daily_loss_usdt': 2.0,
       'max_risk_usdt': 0.2, 'max_stop_fraction': 0.03,
       'fee_rate': 0.001, 'slippage_rate': 0.0005}
FILTERS = {'min_notional': 4.9, 'max_notional': 100000.,
           'min_qty': 0.00001, 'max_qty': 10000., 'step_size': 0.00001}


def signal(name='TREND_BREAKOUT', symbol='BTCUSDT'):
    return dict(strategy=name, symbol=symbol, price=100., stop=98.5,
                target=103., bar_time=123, reason='test')


class TestMultiPaper(unittest.TestCase):
    def test_strategy_registry_is_multiple(self):
        self.assertEqual(len(mod.STRATEGIES), 3)

    def test_one_signal_per_symbol_and_priority(self):
        st = mod.initial_state(CFG)
        picks = mod.pick_signals([signal('RANGE_REVERSION'), signal('TREND_BREAKOUT')],
                                 ['TREND_BREAKOUT', 'RANGE_REVERSION'], st)
        self.assertEqual([x['strategy'] for x in picks], ['TREND_BREAKOUT'])

    def test_reject_duplicate_bar(self):
        st = mod.initial_state(CFG)
        st['seen']['TREND_BREAKOUT:BTCUSDT'] = 123
        self.assertEqual(mod.pick_signals([signal()], ['TREND_BREAKOUT'], st), [])

    def test_open_position_requires_exchange_minimum(self):
        st = mod.initial_state(CFG)
        f = {**FILTERS, 'min_notional': 10.0}
        self.assertEqual(mod.open_position(st, signal(), 100., CFG, f), 'EXCHANGE_NOTIONAL_FILTER')
        self.assertEqual(st['positions'], {})

    def test_open_close_accounts_for_fees_and_slippage(self):
        st = mod.initial_state(CFG)
        self.assertEqual(mod.open_position(st, signal(), 100., CFG, FILTERS), 'PAPER_OPENED')
        self.assertEqual(st['positions']['BTCUSDT']['strategy'], 'TREND_BREAKOUT')
        profit = mod.close_position(st, 'BTCUSDT', 100., CFG, 'TEST')
        self.assertLess(profit, 0)
        self.assertAlmostEqual(st['cash_usdt'], 20 + profit, places=8)

    def test_risk_cap(self):
        st = mod.initial_state(CFG)
        cfg = {**CFG, 'max_risk_usdt': 0.01}
        self.assertEqual(mod.open_position(st, signal(), 100., cfg, FILTERS), 'RISK_PER_TRADE')

    def test_daily_loss_cap_blocks_new_entries(self):
        st = mod.initial_state(CFG)
        st['day_pnl'] = -2.0
        self.assertEqual(mod.open_position(st, signal(), 100., CFG, FILTERS), 'DAILY_LOSS_LIMIT')

    def test_existing_symbol_is_not_duplicated(self):
        st = mod.initial_state(CFG)
        mod.open_position(st, signal(), 100., CFG, FILTERS)
        self.assertEqual(mod.open_position(st, signal(), 100., CFG, FILTERS), 'POSITION_ALREADY_OPEN')

    def test_no_trade_without_valid_levels(self):
        self.assertIsNone(mod.candidate('A', 'BTCUSDT', 100, 101, 105, 1, 'bad'))

    def test_never_submit_live_orders(self):
        self.assertFalse(hasattr(mod, 'place_order'))
        with patch.object(mod, 'market_request', side_effect=AssertionError('no network needed')):
            self.assertEqual(mod.initial_state(CFG)['mode'], 'PAPER_ONLY')

if __name__ == '__main__':
    unittest.main()
