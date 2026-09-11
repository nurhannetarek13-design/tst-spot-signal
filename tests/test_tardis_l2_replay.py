import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))

from tardis_l2_replay import parse_ordered_raw, replay_ordered_rows


def row(line, data, prefix="2021-09-01T00:00:00.0000000Z", stream="x"):
    return {"line": line, "prefix": prefix, "stream": stream, "data": data}


def snapshot(update_id=100):
    return row(
        0,
        {
            "lastUpdateId": update_id,
            "bids": [["100", "2"], ["99", "3"]],
            "asks": [["101", "4"], ["102", "5"]],
        },
        stream="btcusdt@depthSnapshot",
    )


def depth(line, U, u, pu, bids=None, asks=None):
    return row(
        line,
        {"e": "depthUpdate", "U": U, "u": u, "pu": pu, "b": bids or [], "a": asks or []},
        stream="btcusdt@depth",
    )


def ticker(line, u, bid, bid_qty, ask, ask_qty):
    return row(
        line,
        {"u": u, "b": bid, "B": bid_qty, "a": ask, "A": ask_qty},
        stream="btcusdt@bookTicker",
    )


class ParseOrderedRawTests(unittest.TestCase):
    def test_unwraps_data_and_preserves_prefix_and_order(self):
        body = (
            '2021-09-01T00:00:00.1Z {"stream":"a","data":{"u":1}}\n'
            '2021-09-01T00:00:00.2Z {"stream":"b","data":{"u":2}}\n'
        )
        rows = parse_ordered_raw(body)
        self.assertEqual([r["line"] for r in rows], [0, 1])
        self.assertEqual(rows[0]["prefix"], "2021-09-01T00:00:00.1Z")
        self.assertEqual(rows[1]["stream"], "b")
        self.assertEqual(rows[1]["data"]["u"], 2)


class CanonicalReplayTests(unittest.TestCase):
    def test_valid_bridge_uses_pu_even_when_U_is_far_above_snapshot_plus_one(self):
        rows = [
            snapshot(100),
            depth(1, 500, 510, 100, bids=[["100", "3"]]),
            ticker(2, 510, "100", "3", "101", "4"),
        ]
        result = replay_ordered_rows(rows, symbol="BTCUSDT")
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["canonicalReplayReady"])
        self.assertEqual(result["firstBridge"]["U"], 500)
        self.assertEqual(result["firstBridge"]["pu"], 100)
        self.assertEqual(result["tickerExact"], 1)

    def test_continuity_across_multiple_updates(self):
        rows = [
            snapshot(100),
            depth(1, 500, 510, 100, bids=[["100", "3"]]),
            depth(2, 511, 520, 510, asks=[["101", "6"]]),
            ticker(3, 520, "100", "3", "101", "6"),
        ]
        result = replay_ordered_rows(rows, symbol="BTCUSDT")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["eventsApplied"], 2)
        self.assertEqual(result["continuityChecks"], 2)
        self.assertEqual(result["finalDepthU"], 520)

    def test_rejects_bad_initial_snapshot_bridge(self):
        rows = [snapshot(100), depth(1, 500, 510, 99)]
        result = replay_ordered_rows(rows, symbol="BTCUSDT")
        self.assertEqual(result["status"], "SNAPSHOT_PU_BRIDGE_FAIL")
        self.assertFalse(result["canonicalReplayReady"])

    def test_rejects_pu_gap_after_start(self):
        rows = [
            snapshot(100),
            depth(1, 500, 510, 100),
            depth(2, 511, 520, 509),
        ]
        result = replay_ordered_rows(rows, symbol="BTCUSDT")
        self.assertEqual(result["status"], "PU_GAP")
        self.assertEqual(result["prev_u"], 510)
        self.assertEqual(result["pu"], 509)

    def test_rejects_crossed_book(self):
        rows = [
            snapshot(100),
            depth(1, 500, 510, 100, bids=[["101", "7"]]),
        ]
        result = replay_ordered_rows(rows, symbol="BTCUSDT")
        self.assertEqual(result["status"], "CROSSED_BOOK")
        self.assertFalse(result["canonicalReplayReady"])

    def test_reports_book_ticker_mismatch(self):
        rows = [
            snapshot(100),
            depth(1, 500, 510, 100, bids=[["100", "3"]]),
            ticker(2, 510, "100", "999", "101", "4"),
        ]
        result = replay_ordered_rows(rows, symbol="BTCUSDT")
        self.assertEqual(result["status"], "BOOK_TICKER_PARITY_FAIL")
        self.assertEqual(result["tickerComparable"], 1)
        self.assertEqual(result["tickerMismatches"], 1)
        self.assertFalse(result["canonicalReplayReady"])
        self.assertIsNotNone(result["firstMismatch"])


if __name__ == "__main__":
    unittest.main()
