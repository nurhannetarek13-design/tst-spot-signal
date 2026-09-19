import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ready_bot'))
import arbitration as arb


class LegacyLockMigration(unittest.TestCase):
    def state(self):
        return dict(positions={}, closed_trades=[], accepted_signals=[],
                    seen={'TREND_BREAKOUT:FILUSDT': 123},
                    seen_symbol={'FILUSDT': 123})

    def test_old_rejected_lock_is_cleared_but_original_rejection_is_preserved(self):
        state = self.state()
        self.assertEqual(arb.reconcile_legacy_symbol_locks(state), 1)
        self.assertEqual(state['seen_symbol'], {})
        self.assertEqual(state['seen']['TREND_BREAKOUT:FILUSDT'], 123)
        self.assertEqual(state['symbol_lock_schema_version'], 2)
        self.assertEqual(arb.reconcile_legacy_symbol_locks(state), 0)

    def test_valid_other_strategy_can_try_without_retrying_old_rejection(self):
        state = self.state()
        filters = dict(min_notional=5, max_notional=1000, min_qty=.01,
                       max_qty=100000, step_size=.01)
        quotes = {'FILUSDT': dict(price=100, ask=100.02, filters=filters)}
        rejected = dict(strategy='TREND_BREAKOUT', symbol='FILUSDT', bar_time=123)
        independent = dict(strategy='TREND_PULLBACK', symbol='FILUSDT', bar_time=123)
        def open_fn(account, signal, price, config, market_filters):
            account['positions'][signal['symbol']] = dict(strategy=signal['strategy'])
            return 'PAPER_OPENED'
        blocked, opened = arb.execute_candidates([rejected, independent],
            ['TREND_BREAKOUT', 'TREND_PULLBACK'], state, {'trade_size_usdt': 7},
            quotes, open_fn)
        self.assertEqual(blocked, [])
        self.assertEqual([x['strategy'] for x in opened], ['TREND_PULLBACK'])
        self.assertEqual(state['seen_symbol']['FILUSDT'], 123)

    def test_do_not_clear_if_any_open_or_closed_trade_or_missing_proof(self):
        for field, value in [('positions', {'FILUSDT': {'strategy': 'A'}}),
                             ('closed_trades', [{'symbol': 'FILUSDT'}]),
                             ('accepted_signals', None)]:
            with self.subTest(field=field):
                state = self.state()
                state[field] = value
                self.assertEqual(arb.reconcile_legacy_symbol_locks(state), 0)
                self.assertEqual(state['seen_symbol']['FILUSDT'], 123)

    def test_no_migration_when_already_versioned(self):
        state = self.state()
        state['symbol_lock_schema_version'] = 2
        self.assertEqual(arb.reconcile_legacy_symbol_locks(state), 0)
        self.assertEqual(state['seen_symbol']['FILUSDT'], 123)


if __name__ == '__main__':
    unittest.main()
