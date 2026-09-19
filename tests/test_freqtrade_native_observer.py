"""Dependency-free tests for Freqtrade native observation boundary."""
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'freqtrade/user_data/native_signal_observer.py'
SPEC = importlib.util.spec_from_file_location('freqtrade_native_observer', SOURCE)
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


def kline(start=1_000, end=1_899, close='100', high='101', low='99'):
    return [start, '100', high, low, close, '10', end]


class NativeObservationBoundary(unittest.TestCase):
    def test_no_open_candle_lookahead(self):
        result = observer.finished_klines([kline(), kline(1_900, 2_799)], 2_799)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['open_time'], 1_000)
        self.assertEqual(result[0]['close'], 100.)
        self.assertEqual(result[0]['date'].tzinfo.utcoffset(result[0]['date']).total_seconds(), 0)

    def test_non_monotonic_and_invalid_candles_fail(self):
        with self.assertRaisesRegex(ValueError, 'NON_MONOTONIC'):
            observer.finished_klines([kline(), kline()], 10_000)
        with self.assertRaisesRegex(ValueError, 'INVALID_CANDLE'):
            observer.finished_klines([kline(close='102')], 10_000)

    def test_only_spot_approved_universe_and_history(self):
        with self.assertRaisesRegex(ValueError, 'UNAPPROVED'):
            observer.public_klines('BTCUSDTPERP')
        with self.assertRaisesRegex(ValueError, 'UNAPPROVED'):
            observer.public_klines('BTCUSDT', limit=250)

    def test_report_rejects_live_switch_and_does_not_fetch(self):
        with patch.dict(os.environ, {'LIVE_TRADING': 'true'}), patch.object(observer, 'public_klines') as fetch:
            with self.assertRaisesRegex(RuntimeError, 'LIVE_MODE_FORBIDDEN'):
                observer.main()
            fetch.assert_not_called()

    def test_missing_history_fails_without_constructing_strategy(self):
        with self.assertRaisesRegex(RuntimeError, 'INSUFFICIENT_FINISHED'):
            observer.evaluate('BTCUSDT', [], *observer.STRATEGIES[0])

    def test_registry_declares_only_original_15m_sources(self):
        self.assertEqual([x[0] for x in observer.STRATEGIES],
                         ['TST_ALLIGATOR_SMC_V2', 'BASTION_JOAT_SPOT_V2'])
        for _, module, _ in observer.STRATEGIES:
            self.assertTrue((ROOT / 'freqtrade/user_data/strategies' / (module + '.py')).is_file())
        text = SOURCE.read_text()
        for term in ('create_order(', 'place_order(', 'submit_order(', 'apiKey', 'api_secret'):
            self.assertNotIn(term, text)

    def test_nonfinite_metrics_never_serialized(self):
        self.assertIsNone(observer.safe_number(float('nan')))
        self.assertIsNone(observer.safe_number(float('inf')))
        self.assertEqual(observer.safe_number(1.5), 1.5)


if __name__ == '__main__':
    unittest.main()
