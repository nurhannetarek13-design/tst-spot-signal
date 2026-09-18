import unittest
from v2_bot.crash_fvg_profit_audit import PERIODS
class AuditPeriodGuard(unittest.TestCase):
    def test_three_frozen_periods(self):
        self.assertEqual(len(PERIODS),3)
