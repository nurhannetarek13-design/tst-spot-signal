import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ready_bot'))
import multi_bot as core
import paper_audit as audit


class PaperAuditTests(unittest.TestCase):
    def state(self):
        state = core.initial_state({'starting_cash_usdt': 20})
        state['source_registry'] = [
            dict(id='TREND_BREAKOUT', status='PAPER_UNPROVEN'),
            dict(id='TREND_PULLBACK', status='PAPER_UNPROVEN'),
            dict(id='UNIFIED_CANDIDATE', status='BLOCKED_DEPENDENCY_OR_PARITY'),
        ]
        return state

    def test_rejected_legacy_symbol_lock_cleared_but_attempt_history_preserved(self):
        state = self.state()
        state['seen'] = {'TREND_BREAKOUT:FILUSDT': 1000}
        state['seen_symbol'] = {'FILUSDT': 1000}
        fixed = audit.reconcile_legacy_locks(state)
        self.assertEqual([x['symbol'] for x in fixed], ['FILUSDT'])
        self.assertEqual(state['seen_symbol'], {})
        self.assertEqual(state['seen']['TREND_BREAKOUT:FILUSDT'], 1000)
        self.assertEqual(audit.reconcile_legacy_locks(state), [])

    def test_never_clear_locks_from_actual_open_or_historical_closed_trade(self):
        state = self.state()
        state['seen_symbol'] = {'BTCUSDT': 1000, 'ETHUSDT': 2000, 'FILUSDT': 3000}
        state['positions']['BTCUSDT'] = {'strategy': 'TREND_BREAKOUT'}
        state['closed_trades'].append({'symbol': 'ETHUSDT', 'strategy': 'TREND_PULLBACK'})
        audit.reconcile_legacy_locks(state)
        self.assertEqual(state['seen_symbol'], {'BTCUSDT': 1000, 'ETHUSDT': 2000})

    def test_report_counts_each_candle_once_without_fabricating_trades(self):
        state = self.state()
        state['signals'] = [dict(strategy='TREND_BREAKOUT', symbol='FILUSDT', bar_time=1000,
                                 reason='breakout')]
        state['blocked'] = [dict(strategy='TREND_BREAKOUT', symbol='FILUSDT',
                                 reason='INVALID_STOP_DISTANCE')]
        report = audit.record_run(state, 0)
        first = report['by_strategy'][0]
        self.assertEqual((first['signals'], first['opened'], first['rejected'], first['closed']),
                         (1, 0, 1, 0))
        self.assertEqual(first['rejection_reasons'], {'INVALID_STOP_DISTANCE': 1})
        self.assertEqual(first['realized_pnl_usdt'], 0)
        audit.record_run(state, 0)
        self.assertEqual(state['paper_report']['by_strategy'][0]['signals'], 1)
        self.assertEqual(state['paper_report']['by_strategy'][0]['rejected'], 1)
        self.assertEqual(state['paper_report']['by_strategy'][2]['opened'], 0)

    def test_accepted_and_closed_trades_record_actual_fee_adjusted_pnl_once(self):
        state = self.state()
        state['signals'] = [dict(strategy='TREND_PULLBACK', symbol='NEARUSDT', bar_time=1000,
                                 reason='reclaim')]
        state['accepted_signals'] = [dict(strategy='TREND_PULLBACK', symbol='NEARUSDT',
                                          bar_time=1000, reason='PAPER_OPENED')]
        audit.record_run(state, 0)
        state['accepted_signals'] = []
        state['closed_trades'] = [dict(strategy='TREND_PULLBACK', symbol='NEARUSDT',
                                       opened_at='2026-09-19T00:00:00+00:00',
                                       closed_at='2026-09-19T03:00:00+00:00',
                                       pnl_usdt=-0.11)]
        report = audit.record_run(state, 0)
        row = report['by_strategy'][1]
        self.assertEqual((row['signals'], row['opened'], row['closed']), (1, 1, 1))
        self.assertEqual(row['realized_pnl_usdt'], -0.11)
        audit.record_run(state, 0)
        self.assertEqual(state['paper_report']['by_strategy'][1]['closed'], 1)

    def test_historical_trades_are_not_claimed_when_adding_audit(self):
        state = self.state()
        state['closed_trades'] = [dict(strategy='TREND_BREAKOUT', symbol='BTCUSDT',
                                        opened_at='old', closed_at='old', pnl_usdt=1)]
        report = audit.record_run(state, 1)
        self.assertEqual(report['by_strategy'][0]['closed'], 0)
        self.assertIsNotNone(report['since'])

    def test_no_market_data_error_spam_on_same_utc_day(self):
        state = self.state()
        state['blocked'] = [dict(symbol='BTCUSDT', reason='DATA_OR_FILTER_UNAVAILABLE')]
        audit.record_run(state, 0)
        audit.record_run(state, 0)
        self.assertEqual(state['paper_audit']['stats']['_SYSTEM']['rejected'], 1)


if __name__ == '__main__':
    unittest.main()
