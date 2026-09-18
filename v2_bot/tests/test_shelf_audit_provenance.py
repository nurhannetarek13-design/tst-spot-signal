import unittest
from v2_bot.crash_fvg_profit_audit import START, END, PERIODS, ms
from v2_bot.shelf_profit_audit import SOURCE_PIN


class ShelfProvenanceTests(unittest.TestCase):
    def test_predeclared_periods_cover_entire_horizon_without_overlap(self):
        self.assertEqual(PERIODS[0][0],START)
        self.assertEqual(PERIODS[-1][1],END)
        for left,right in zip(PERIODS,PERIODS[1:]):
            self.assertEqual(left[1],right[0])
            self.assertLess(ms(left[0]),ms(left[1]))
        self.assertEqual(len(SOURCE_PIN),40)


if __name__=='__main__':
    unittest.main()
