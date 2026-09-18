import unittest
from v2_bot.pionex_style import FEE, SLIPPAGE
from v2_bot.crash_fvg_profit_audit import ASSUMED_FULL_SPREAD


class ShelfCostsTests(unittest.TestCase):
    def test_realistic_costs_are_not_zero(self):
        self.assertGreater(FEE,0)
        self.assertGreater(SLIPPAGE,0)
        self.assertGreater(ASSUMED_FULL_SPREAD,0)
        self.assertGreater(2*(FEE+SLIPPAGE)+ASSUMED_FULL_SPREAD,.003)


if __name__=='__main__':
    unittest.main()
