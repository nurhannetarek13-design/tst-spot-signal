"""Do not represent observed drawdown checks as exchange-protected stops."""
import unittest
from v2_bot.pionex_style import Rules, new_state, _buy, step


class ExistingShelfRiskTests(unittest.TestCase):
    def test_existing_breakout_can_gap_beyond_two_dollar_cap(self):
        rules=Rules(5.,.00001,.00001)
        state=new_state('trend_breakout','BTCUSDT',50.)
        self.assertEqual(_buy(state,100.,15.,rules,'test_prior_entry'),'paper_buy')
        prior=900000*200
        state['last_bar']=prior
        bars=[{'open_time':prior+900000*(i-79), 'open':90.,'high':91.,
               'low':89.,'close':90.,'quote_volume':100000.,'taker_buy_quote':60000.}
              for i in range(80)]
        # Same complete closed bar timestamp exactly one candle after entry.
        # Deliberate 20% gap makes protective $2 assumption fail. For the
        # unit test only, exit has to be at OBSERVED quote, never at stop.
        for i,bar in enumerate(bars):
            bar['open_time']=prior-900000*(79-i)+900000
        hours=[{'close':90.} for _ in range(80)]
        result=step(state,bars,hours,{'bid':80.,'ask':80.03},rules)
        self.assertTrue(state['halted'])
        self.assertLess(result['total_pnl_including_unrealized_usdt'],-2.)
        self.assertFalse(result['live_trading'])
        self.assertEqual(result['actual_orders'],0)


if __name__=='__main__':
    unittest.main()
