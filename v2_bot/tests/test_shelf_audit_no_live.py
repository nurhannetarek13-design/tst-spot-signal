import unittest
from v2_bot.pionex_style import new_state, snapshot

class NoLiveTests(unittest.TestCase):
    def test_existing_paper_snapshot_remains_zero_real_orders(self):
        for mode in ('spot_grid','fixed_dca','trend_breakout'):
            state=new_state(mode,'BTCUSDT',50.)
            snap=snapshot(state,{'bid':100.,'ask':100.03},'none')
            self.assertEqual(snap['actual_orders'],0)
            self.assertFalse(snap['live_trading'])
            self.assertEqual(snap['equity_usdt'],50.)

if __name__=='__main__':
    unittest.main()
