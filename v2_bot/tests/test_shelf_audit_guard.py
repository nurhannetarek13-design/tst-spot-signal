import unittest
from v2_bot.pionex_style import Rules, new_state, step


class ShelfExecutionGuard(unittest.TestCase):
    def test_flat_data_has_no_breakout_order_and_is_idempotent(self):
        state=new_state('trend_breakout','BTCUSDT',50.)
        bars=[{'open_time':900000*i,'open':100.,'high':100.5,
               'low':99.5,'close':100.,'quote_volume':100000.,
               'taker_buy_quote':60000.} for i in range(120)]
        hourly=[{'close':100.} for _ in range(80)]
        book={'bid':99.985,'ask':100.015}
        rules=Rules(5.,.00001,.00001)
        first=step(state,bars,hourly,book,rules)
        self.assertEqual(first['trades'],0)
        again=step(state,bars,hourly,book,rules)
        self.assertEqual(again['action'],'duplicate_bar_no_action')
        self.assertEqual(again['trades'],0)
        self.assertFalse(again['live_trading'])


if __name__=='__main__':
    unittest.main()
