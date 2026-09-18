"""Observed ROI or win rate alone is never acceptance of a live bot."""
import unittest
from unittest.mock import patch
from v2_bot.shelf_profit_audit import audit_one
from v2_bot.crash_fvg_profit_audit import START, BAR, ms
from v2_bot.historical_replay import resample_15m
from v2_bot.pionex_style import Rules


class ShelfLiveGateTests(unittest.TestCase):
    def test_one_lucky_profit_not_live_evidence(self):
        rows=[]
        for i in range(300):
            rows.append({'open_time':ms(START)+i*BAR,'close_time':ms(START)+(i+1)*BAR-1,
                'open':100.,'high':101.,'low':99.,'close':100.,
                'quote_volume':100000.,'taker_buy_quote':60000.,'volume':100.})
        hours=resample_15m(rows,4)
        closes=[int(x['close_time']) for x in hours]
        row=audit_one(rows,hours,closes,'BTCUSDT','fixed_dca',
                      Rules(5.,.00001,.00001),START,'2025-09-04')
        self.assertFalse(row['descriptive_sample_gate'])
        self.assertEqual(row['closed_trades'],0)
        self.assertFalse(row['halted'])
        self.assertEqual(row['wins'],0)


if __name__=='__main__':
    unittest.main()
