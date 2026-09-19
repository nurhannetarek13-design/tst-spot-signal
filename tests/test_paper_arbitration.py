import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ready_bot'))
import arbitration
import multi_bot as core
import native_paper as runner

CFG = dict(starting_cash_usdt=20., trade_size_usdt=5., max_open_positions=3,
           max_daily_loss_usdt=2., max_risk_usdt=.2,
           max_portfolio_risk_usdt=.6, max_stop_fraction=.03,
           fee_rate=.001, slippage_rate=.0005)
FILTERS = dict(min_notional=4.9, max_notional=10000.,
               min_qty=.00001, max_qty=10000., step_size=.00001)


def signal(name, stop=99., symbol='BTCUSDT', bar=1000):
    return dict(strategy=name, symbol=symbol, price=100., stop=stop,
                target=102., bar_time=bar, reason='contract test')


def market(ask=100.):
    return {'BTCUSDT': dict(price=100., ask=ask, bid=ask-.02, filters=FILTERS)}


class ArbitrationContract(unittest.TestCase):
    def test_risk_rejected_first_strategy_allows_other_independent_strategy(self):
        state = core.initial_state(CFG)
        order = ['TREND_BREAKOUT', 'TREND_PULLBACK']
        blocked, opened = arbitration.execute_candidates([
            signal('TREND_BREAKOUT', stop=95.), signal('TREND_PULLBACK')
        ], order, state, CFG, market(), core.open_position)
        self.assertEqual(blocked[0]['reason'], 'INVALID_STOP_DISTANCE')
        self.assertEqual([s['strategy'] for s in opened], ['TREND_PULLBACK'])
        self.assertEqual(state['positions']['BTCUSDT']['strategy'], 'TREND_PULLBACK')
        self.assertEqual(state['seen_symbol']['BTCUSDT'], 1000)

    def test_first_accepted_strategy_blocks_second_on_same_symbol(self):
        state = core.initial_state(CFG)
        blocked, opened = arbitration.execute_candidates([
            signal('TREND_BREAKOUT'), signal('TREND_PULLBACK')
        ], ['TREND_BREAKOUT', 'TREND_PULLBACK'], state, CFG, market(), core.open_position)
        self.assertFalse(blocked)
        self.assertEqual(len(opened), 1)
        self.assertNotIn('TREND_PULLBACK:BTCUSDT', state['seen'])

    def test_failed_signal_never_consumes_symbol_lock(self):
        state = core.initial_state(CFG)
        blocked, opened = arbitration.execute_candidates([
            signal('TREND_BREAKOUT', stop=95.)
        ], ['TREND_BREAKOUT'], state, CFG, market(), core.open_position)
        self.assertFalse(opened)
        self.assertNotIn('BTCUSDT', state['seen_symbol'])
        self.assertEqual(blocked[0]['reason'], 'INVALID_STOP_DISTANCE')

    def test_no_retry_of_rejected_signal_on_same_bar(self):
        state = core.initial_state(CFG)
        state['seen']['TREND_BREAKOUT:BTCUSDT'] = 1000
        blocked, opened = arbitration.execute_candidates([
            signal('TREND_BREAKOUT')
        ], ['TREND_BREAKOUT'], state, CFG, market(), core.open_position)
        self.assertEqual((blocked, opened), ([], []))

    def test_uses_best_ask_not_last_trade_for_paper_buy(self):
        state = core.initial_state(CFG)
        _, opened = arbitration.execute_candidates([signal('TREND_BREAKOUT')],
            ['TREND_BREAKOUT'], state, CFG, market(ask=100.5), core.open_position)
        self.assertEqual(len(opened), 1)
        self.assertAlmostEqual(state['positions']['BTCUSDT']['entry'], 100.5 * 1.0005)

    def test_persists_native_hold_and_rejects_invalid_contract(self):
        runner.register_native()
        state = core.initial_state(CFG)
        s = signal(runner.SMALL_CAP)
        s.update(hold_bars=8, hold_interval='15m', native_source='ORIGINAL',
                 max_stake_usdt=5.)
        _, opened = arbitration.execute_candidates([s], [runner.SMALL_CAP], state,
                                                     CFG, market(), core.open_position)
        self.assertEqual(len(opened), 1)
        position = state['positions']['BTCUSDT']
        self.assertEqual((position['hold_bars'], position['hold_interval'], position['native_source']),
                         (8, '15m', 'ORIGINAL'))
        state2 = core.initial_state(CFG)
        s['hold_bars'] = -1
        blocked, opened = arbitration.execute_candidates([s], [runner.SMALL_CAP], state2,
                                                          CFG, market(), core.open_position)
        self.assertFalse(opened)
        self.assertEqual(blocked[0]['reason'], 'INVALID_NATIVE_HOLD_CONTRACT')

    def test_time_exit_uses_fill_time_and_only_completed_candles(self):
        opened_ms = 10_000_000
        pos = dict(opened_at=datetime.fromtimestamp(opened_ms/1000, tz=timezone.utc).isoformat(),
                   bar_time=opened_ms-900_000, hold_interval='15m', hold_bars=8)
        interval = 900_000
        self.assertFalse(runner.native_time_exit_due(pos, [dict(open_time=opened_ms+7*interval-1)]))
        self.assertTrue(runner.native_time_exit_due(pos, [dict(open_time=opened_ms+7*interval)]))
        with self.assertRaisesRegex(RuntimeError, 'CONTRACT_OR_DATA_MISSING'):
            runner.native_time_exit_due(dict(pos, hold_interval='unknown'), [])


if __name__ == '__main__':
    unittest.main()
