"""Safety regressions for the frozen existing-shelf diagnostic."""
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
            o=100.0
            bars.append({'open_time':ms(START)+i*BAR,'open':o,'high':o*1.002,
                         'low':o*.998,'close':o,'volume':100.,
                         'quote_volume':100000.,'taker_buy_quote':60000.,
                         'close_time':ms(START)+(i+1)*BAR-1})
        hours=resample_15m(bars,4)
        return bars,hours,[int(x['close_time']) for x in hours]

    def test_uses_previous_closed_signal_and_NEXT_open_only(self):
        bars,hours,closes=self._market()
        # Inject an obviously different future next-open quote; signals must
        # still see closed current candle and only execution proxy moves.
        next_idx=225
        bars[next_idx]['open']=105.0
        bars[next_idx]['high']=106.0
        bars[next_idx]['low']=99.0
        bars[next_idx]['close']=105.0
        # hourly precalculation must match changed candles; no future hour
        # should leak into an earlier completed-bar signal.
        hours=resample_15m(bars,4)
        closes=[int(x['close_time']) for x in hours]
        with patch('v2_bot.shelf_profit_audit.step') as decision:
            def inspect(state,candles,hourly,book,rules):
                self.assertLess(int(candles[-1]['open_time']),int(bars[next_idx]['open_time'])) if int(candles[-1]['open_time'])==int(bars[next_idx-1]['open_time']) else None
                if int(candles[-1]['open_time'])==int(bars[next_idx-1]['open_time']):
                    self.assertAlmostEqual(book['ask'],105*(1+.0003/2))
                    self.assertEqual(candles[-1]['close'],bars[next_idx-1]['close'])
                state['last_bar']=int(candles[-1]['open_time'])
                return {'action':'no_action'}
            decision.side_effect=inspect
            result=audit_one(bars,hours,closes,'BTCUSDT','trend_breakout',
                             Rules(5.,.00001,.00001),START,'2025-09-04')
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
