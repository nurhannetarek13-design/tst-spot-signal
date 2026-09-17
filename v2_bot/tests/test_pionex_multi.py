from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from v2_bot.pionex_multi import SYMBOLS, run_all, state_path
from v2_bot.tests.test_pionex_style import BOOK, FakePublic


class ThreeSymbolPublic(FakePublic):
    def book_tickers(self):
        return {symbol: dict(BOOK) for symbol in SYMBOLS}


class MultiSymbolPaperTests(unittest.TestCase):
    def test_independent_nine_accounts_restore_without_duplicate_fills(self):
        fake = ThreeSymbolPublic()
        now = int(fake.candles[-1]['open_time']) + 900_100
        with tempfile.TemporaryDirectory() as tmp:
            first = run_all(client=fake, state_dir=tmp, now_ms=now)
            self.assertEqual(first['virtual_scenarios'], 9)
            self.assertEqual(first['real_orders'], 0)
            self.assertFalse(first['live_trading'])
            self.assertTrue(first['independent_scenarios_not_a_pooled_portfolio'])
            self.assertTrue(first['do_not_sum_hypothetical_balances'])
            self.assertEqual(set(first['by_symbol']), set(SYMBOLS))
            self.assertEqual(len({state_path(tmp, symbol) for symbol in SYMBOLS}), 3)
            for symbol in SYMBOLS:
                self.assertTrue(Path(state_path(tmp, symbol)).is_file())
                self.assertFalse(first['by_symbol'][symbol]['prior_ledger_restored'])
                self.assertEqual(first['by_symbol'][symbol]['virtual_budget_per_scenario'], 50)
            second = run_all(client=fake, state_dir=tmp, now_ms=now)
            for symbol in SYMBOLS:
                row = second['by_symbol'][symbol]
                self.assertTrue(row['prior_ledger_restored'])
                self.assertTrue(all(v['action'] == 'duplicate_bar_no_action'
                                    for v in row['strategies'].values()))
                for mode, virtual_account in row['strategies'].items():
                    earlier = first['by_symbol'][symbol]['strategies'][mode]
                    self.assertEqual(virtual_account['trades'], earlier['trades'])
                    self.assertEqual(virtual_account['equity_usdt'], earlier['equity_usdt'])
                    self.assertEqual(virtual_account['fees_paid_usdt'], earlier['fees_paid_usdt'])

    def test_corrupt_one_symbol_halts_batch_instead_of_resetting_its_state(self):
        fake = ThreeSymbolPublic()
        now = int(fake.candles[-1]['open_time']) + 900_100
        with tempfile.TemporaryDirectory() as tmp:
            run_all(client=fake, state_dir=tmp, now_ms=now)
            target = Path(state_path(tmp, 'ETHUSDT'))
            payload = json.loads(target.read_text())
            original_cash = payload['strategies']['spot_grid']['cash']
            payload['strategies']['spot_grid']['cash'] += 17
            target.write_text(json.dumps(payload))
            with self.assertRaisesRegex(RuntimeError, 'paper_batch_failed_at_ETHUSDT'):
                run_all(client=fake, state_dir=tmp, now_ms=now)
            self.assertEqual(json.loads(target.read_text())['strategies']['spot_grid']['cash'], original_cash + 17)

    def test_mismatched_market_bar_times_fail_without_aggregate_report(self):
        def inconsistent(*, symbol, **kwargs):
            return {'real_orders': 0, 'live_trading': False,
                    'virtual_budget_per_scenario': 50.,
                    'strategies': {'spot_grid': {}, 'fixed_dca': {}, 'trend_breakout': {}},
                    'last_closed_bar': 900_000 if symbol != 'SOLUSDT' else 1_800_000}
        with patch('v2_bot.pionex_multi.run_once', side_effect=inconsistent):
            with self.assertRaisesRegex(RuntimeError, 'symbol_candle_times_disagree'):
                run_all(client=object())

    def test_unsupported_symbol_cannot_collide_with_existing_state(self):
        with self.assertRaisesRegex(ValueError, 'unsupported_symbol'):
            state_path('/tmp/v2-paper', 'BNBUSDT')


if __name__ == '__main__':
    unittest.main()
