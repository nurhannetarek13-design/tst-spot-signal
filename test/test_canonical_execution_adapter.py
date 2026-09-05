import importlib.util
import pathlib
import unittest

import pandas as pd


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "canonical-execution-adapter.py"
spec = importlib.util.spec_from_file_location("canonical_execution_adapter", MODULE_PATH)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def frame(rows):
    idx = pd.date_range("2026-01-01T00:00:00Z", periods=len(rows), freq="1h")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


class CanonicalExecutionAdapterTests(unittest.TestCase):
    def test_next_bar_open_and_sl_first_collision(self):
        df = frame([
            [90, 91, 89, 90],
            [100, 107, 96, 102],
            [102, 103, 101, 102],
        ])
        trades = adapter.simulate(
            df,
            {df.index[0]},
            stake=5.5,
            fee=0.0015,
            sl=0.03,
            tp=0.06,
            hold=24,
        )
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["entryTs"], df.index[1].isoformat())
        self.assertEqual(t["entryPrice"], 100.0)
        self.assertEqual(t["exitTs"], df.index[1].isoformat())
        self.assertAlmostEqual(t["exitPrice"], 97.0, places=12)
        self.assertEqual(t["reason"], "SL_AMBIGUOUS_CONSERVATIVE")

    def test_signals_through_exit_bar_are_suppressed(self):
        df = frame([
            [100, 101, 99, 100],
            [100, 107, 99, 106],
            [106, 107, 105, 106],
            [110, 111, 109, 110],
            [110, 111, 109, 110],
        ])
        # t0 creates trade entered/exited on t1 via TP. The t1 signal must be ignored.
        # t2 remains eligible and must enter on t3.
        trades = adapter.simulate(
            df,
            {df.index[0], df.index[1], df.index[2]},
            stake=5.5,
            fee=0.0015,
            sl=0.03,
            tp=0.06,
            hold=1,
        )
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["signalTs"], df.index[0].isoformat())
        self.assertEqual(trades[1]["signalTs"], df.index[2].isoformat())
        self.assertEqual(trades[1]["entryTs"], df.index[3].isoformat())

    def test_time_exit_is_entry_plus_hold_bars_close(self):
        df = frame([
            [100, 101, 99, 100],
            [100, 102, 99, 101],
            [101, 102, 100, 101.5],
            [101.5, 102, 100.5, 101.25],
            [101.25, 102, 100, 101],
        ])
        trades = adapter.simulate(
            df,
            {df.index[0]},
            stake=5.5,
            fee=0.0015,
            sl=0.10,
            tp=0.10,
            hold=2,
        )
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["entryTs"], df.index[1].isoformat())
        self.assertEqual(t["exitTs"], df.index[3].isoformat())
        self.assertEqual(t["exitPrice"], float(df.close.iloc[3]))
        self.assertEqual(t["reason"], "TIME")


if __name__ == "__main__":
    unittest.main()
