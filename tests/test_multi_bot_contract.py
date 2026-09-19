import importlib.util
import sys
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / 'ready_bot' / 'multi_bot.py'
spec = importlib.util.spec_from_file_location('multi_bot_contract', MODULE)
bot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bot
spec.loader.exec_module(bot)

CFG = dict(starting_cash_usdt=20., trade_size_usdt=5., max_open_positions=3,
           max_daily_loss_usdt=2., max_risk_usdt=.2,
           max_portfolio_risk_usdt=.6, max_stop_fraction=.03,
           fee_rate=.001, slippage_rate=.0005)
FILTERS = dict(min_notional=4.9, max_notional=100000.,
               min_qty=.00001, max_qty=100000., step_size=.00001)


def signal(strategy='RANGE_REVERSION', symbol='BTCUSDT', target=101.):
    return dict(strategy=strategy, symbol=symbol, price=100., stop=99.,
                target=target, bar_time=1000, reason='unit test')


class TestIndependentStrategyContracts(unittest.TestCase):
    def test_range_mean_target_is_not_replaced_by_two_r(self):
        state = bot.initial_state(CFG)
        sig = signal(target=100.8)
        self.assertEqual(bot.open_position(state, sig, 100., CFG, FILTERS), 'PAPER_OPENED')
        pos = state['positions']['BTCUSDT']
        self.assertAlmostEqual(pos['target'], pos['entry'] * 1.008, places=8)
        self.assertAlmostEqual(pos['stop'], pos['entry'] * .99, places=8)
        self.assertNotAlmostEqual(pos['target'], pos['entry'] + 2 * (pos['entry']-pos['stop']))

    def test_portfolio_risk_is_shared_between_strategies(self):
        state = bot.initial_state(CFG)
        cfg = {**CFG, 'max_portfolio_risk_usdt': .09}
        self.assertEqual(bot.open_position(state, signal(), 100., cfg, FILTERS), 'PAPER_OPENED')
        self.assertEqual(bot.open_position(state, signal('TREND_BREAKOUT', 'ETHUSDT'), 100., cfg, FILTERS),
                         'PORTFOLIO_STOP_RISK')
        self.assertEqual(len(state['positions']), 1)

    def test_target_exit_is_capped_at_strategy_target(self):
        state = bot.initial_state(CFG)
        self.assertEqual(bot.open_position(state, signal(), 100., CFG, FILTERS), 'PAPER_OPENED')
        pos = dict(state['positions']['BTCUSDT'])
        pnl = bot.close_position(state, 'BTCUSDT', 120., CFG, 'TARGET')
        target_net = pos['qty'] * bot.simulated_fill(pos['target'], 'sell', CFG) * (1-CFG['fee_rate'])
        self.assertAlmostEqual(pnl, target_net - pos['cost'], places=8)
        self.assertEqual(state['closed_trades'][-1]['strategy'], 'RANGE_REVERSION')
        self.assertEqual(state['closed_trades'][-1]['reason'], 'TARGET')

    def test_stop_gap_does_not_fabricate_better_exit(self):
        state = bot.initial_state(CFG)
        self.assertEqual(bot.open_position(state, signal(), 100., CFG, FILTERS), 'PAPER_OPENED')
        pos = dict(state['positions']['BTCUSDT'])
        bot.close_position(state, 'BTCUSDT', 90., CFG, 'STOP')
        self.assertLess(state['closed_trades'][-1]['exit'], pos['stop'])

    def test_invalid_mode_and_cost_fail_closed(self):
        full = {**CFG, 'mode': 'live', 'strategies': ['TREND_BREAKOUT'],
                'symbols': ['BTCUSDT']}
        with self.assertRaisesRegex(RuntimeError, 'PAPER'):
            bot.validate_config(full)
        with self.assertRaisesRegex(RuntimeError, 'trading cost'):
            bot.validate_config({**full, 'mode': 'paper', 'fee_rate': -1})

    def test_unknown_strategy_cannot_open(self):
        state = bot.initial_state(CFG)
        self.assertEqual(bot.open_position(state, signal(strategy='UNKNOWN'), 100., CFG, FILTERS),
                         'UNKNOWN_STRATEGY')
        self.assertEqual(state['positions'], {})


if __name__ == '__main__':
    unittest.main()
