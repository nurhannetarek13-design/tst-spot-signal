import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ready_bot'))
import native_paper as bridge


class RealSourceImportTest(unittest.TestCase):
    def test_regime_original_module_rejects_insufficient_history(self):
        result = bridge.native_score('momentum', dict(symbol='BTCUSDT', candles=[], btcCandles=[],
            quoteVolume24h=50_000_000, bid=99.9, ask=100.0,
            takerBuyRatio=.60, relativeVolume=2.0))
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'INSUFFICIENT_HISTORY')
        self.assertEqual(result['strategy'], bridge.NATIVE_SOURCE_IDS[bridge.REGIME])

    def test_small_cap_original_module_rejects_excluded_base(self):
        result = bridge.native_score('small_cap', dict(symbol='BTCUSDT', baseAsset='BTC',
            c15=[], c1h=[], btc15=[], quoteVolume24h=50_000_000, bid=99.9,
            ask=100.0, takerBuyRatio=.60, relativeVolume=2.0))
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'UNSUPPORTED_SYMBOL')
        self.assertEqual(result['strategy'], bridge.SMALL_CAP)


if __name__ == '__main__':
    unittest.main()
