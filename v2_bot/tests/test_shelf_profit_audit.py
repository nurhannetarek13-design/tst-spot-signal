"""Historical replay must only use past closed bars, and never treat no fills as profit."""
from __future__ import annotations
import unittest
from unittest.mock import patch
from v2_bot.shelf_profit_audit import audit_one
from v2_bot.historical_replay import resample_15m
from v2_bot.pionex_style import Rules
from v2_bot.crash_fvg_profit_audit import BAR, START, ms

class ShelfAuditTests(unittest.TestCase):
    def _market(self):
        bars=[]
        for i in range(300):
            o=100.
            bars.append({'open_time':ms(START)+i*BAR,'open':o,'high':o*1.002,
                'low':o*.998,'close':o,'volume':100.,'quote_volume':100000.,
                'trades':100.,'taker_buy_base':60.,'taker_buy_quote':60000.,
                'close_time':ms(START)+(i+1)*BAR-1})
        hours=resample_15m(bars,4)
        return bars,hours,[int(x['close_time']) for x in hours]

    def test_uses_previous_closed_signal_and_NEXT_open_only(self):
        bars,hours,closes=self._market()
        next_idx=225
        bars[next_idx].update(open=105.,high=106.,low=99.,close=105.)
        hours=resample_15m(bars,4)
        closes=[int(x['close_time']) for x in hours]
        observed=[]
        with patch('v2_bot.shelf_profit_audit.step') as decision:
            def inspect(state,candles,hourly,book,rules):
                if int(candles[-1]['open_time'])==int(bars[next_idx-1]['open_time']):
                    observed.append(True)
                    self.assertAlmostEqual(book['ask'],105*(1+.0003/2))
                    self.assertEqual(candles[-1]['close'],bars[next_idx-1]['close'])
                    self.assertLess(int(hourly[-1]['close_time']),
                                    int(bars[next_idx]['open_time'])+BAR)
                state['last_bar']=int(candles[-1]['open_time'])
                return {'action':'no_action'}
            decision.side_effect=inspect
            result=audit_one(bars,hours,closes,'BTCUSDT','trend_breakout',
                Rules(5.,.00001,.00001),START,'2025-09-04')
        self.assertTrue(observed)
        self.assertFalse(result['descriptive_sample_gate'])
        self.assertEqual(result['virtual_buys'],0)

    def test_zero_trades_is_not_profitability(self):
        bars,hours,closes=self._market()
        result=audit_one(bars,hours,closes,'BTCUSDT','trend_breakout',
            Rules(5.,.00001,.00001),START,'2025-09-04')
        self.assertEqual(result['closed_trades'],0)
        self.assertIsNone(result['profit_factor'])
        self.assertFalse(result['descriptive_sample_gate'])
        self.assertEqual(result['marked_net_usdt'],0.)

if __name__=='__main__':
    unittest.main()
