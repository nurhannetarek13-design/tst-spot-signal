import unittest
from v2_bot.shelf_profit_audit import SOURCE_PIN

class ForecastGuard(unittest.TestCase):
    def test_frozen_source_pin(self):
        self.assertEqual(len(SOURCE_PIN),40)

if __name__=='__main__':
    unittest.main()
