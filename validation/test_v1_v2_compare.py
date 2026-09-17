"""Run with: python -m unittest validation/test_v1_v2_compare.py"""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from validation.v1_v2_compare import compare, metrics

M = dict(data_hash='sha256:real-data', start_utc='2025-09-01T00:00:00Z', end_utc='2026-09-01T00:00:00Z',
         oos_start_utc='2026-05-15T00:00:00Z', symbols=['BTCUSDT'], fee_each_side=0.001,
         slippage_each_side=0.0005, starting_capital_usdt=50, closed_candle_only=True,
         next_bar_execution=True, stop_first=True, historical_l2_complete=True,
         strategy_commit='abc123', paper_only=True, replay_complete=True)

class Tests(unittest.TestCase):
    def test_metrics(self):
        x = metrics([{'pnl': 1}, {'pnl': -2}, {'pnl': 1}])
        self.assertEqual(x['trades'], 3)
        self.assertEqual(x['profit_factor'], 1.0)
        self.assertEqual(x['max_closed_equity_drawdown_usdt'], 2)

    def test_gate_rejects_missing_l2(self):
        v1, v2 = M.copy(), M.copy()
        v1['historical_l2_complete'] = False
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root/'a.json').write_text(json.dumps(v1))
            (root/'b.json').write_text(json.dumps(v2))
            result = compare(root/'a.json',root/'missing-a.csv',root/'b.json',root/'missing-b.csv')
            self.assertEqual(result['status'],'NOT_COMPARABLE')
            self.assertIn('v1:historical_l2_complete:NOT_PROVEN',result['reasons'])

    def test_gate_rejects_different_data(self):
        v1, v2 = M.copy(), M.copy()
        v2['data_hash'] = 'different'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root/'a.json').write_text(json.dumps(v1))
            (root/'b.json').write_text(json.dumps(v2))
            self.assertIn('MISMATCH:data_hash',compare(root/'a.json',root/'missing-a.csv',root/'b.json',root/'missing-b.csv')['reasons'])

    def test_comparable_two_oos_trades(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for prefix in ('a','b'):
                (root/f'{prefix}.json').write_text(json.dumps(M))
                with (root/f'{prefix}.csv').open('w',newline='') as f:
                    wr = csv.DictWriter(f,fieldnames=['symbol','entry_time_utc','exit_time_utc','entry_price','exit_price','quote_size_usdt'])
                    wr.writeheader()
                    wr.writerow(dict(symbol='BTCUSDT',entry_time_utc='2026-07-01T00:00:00Z',exit_time_utc='2026-07-01T04:00:00Z',entry_price=100,exit_price=102,quote_size_usdt=10))
            outcome = compare(root/'a.json',root/'a.csv',root/'b.json',root/'b.csv')
            self.assertEqual(outcome['status'],'COMPARABLE_DESCRIPTIVE_ONLY')
            self.assertGreater(outcome['results']['v1']['oos']['net_pnl_usdt'],0)
            self.assertEqual(outcome['results']['v1'], outcome['results']['v2'])

if __name__ == '__main__':
    unittest.main()
