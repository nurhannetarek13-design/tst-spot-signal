import unittest
from v2_bot.crash_fvg_profit_audit import proxy_book

class QuoteProxyTests(unittest.TestCase):
    def test_proxy_is_spread_not_historical_best_quote(self):
        b=proxy_book(100.)
        self.assertLess(b['bid'],100.)
        self.assertGreater(b['ask'],100.)
        self.assertGreater(b['ask'],b['bid'])

if __name__=='__main__':
    unittest.main()
