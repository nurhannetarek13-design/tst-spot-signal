import tempfile
import unittest
from pathlib import Path

from v2_bot.execution_journal import ExecutionJournal


class ExecutionJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "journal.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_pending_execution_survives_reopen(self):
        first = ExecutionJournal(self.path)
        created = first.begin(
            client_order_id="v2-BTCUSDT-123",
            symbol="BTCUSDT",
            details={"quote_size": 10},
        )
        self.assertEqual(created.stage, "INTENT")
        self.assertTrue(first.has_pending())

        reopened = ExecutionJournal(self.path)
        pending = reopened.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].client_order_id, "v2-BTCUSDT-123")
        self.assertEqual(pending[0].details["quote_size"], 10)

    def test_terminal_state_no_longer_blocks_new_execution(self):
        journal = ExecutionJournal(self.path)
        journal.begin(client_order_id="v2-SOLUSDT-1", symbol="SOLUSDT")
        journal.transition("v2-SOLUSDT-1", "BUY_FILLED")
        journal.transition("v2-SOLUSDT-1", "OCO_INTENT")
        journal.transition(
            "v2-SOLUSDT-1",
            "PROTECTED",
            details={"order_list_id": 77},
        )
        self.assertFalse(journal.has_pending())
        record = journal.get("v2-SOLUSDT-1")
        self.assertIsNotNone(record)
        self.assertEqual(record.stage, "PROTECTED")
        self.assertEqual(record.details["order_list_id"], 77)

    def test_unknown_buy_and_unknown_oco_remain_pending(self):
        journal = ExecutionJournal(self.path)
        journal.begin(client_order_id="v2-ETHUSDT-1", symbol="ETHUSDT")
        journal.transition("v2-ETHUSDT-1", "BUY_UNKNOWN")
        self.assertTrue(journal.has_pending())
        journal.transition("v2-ETHUSDT-1", "BUY_FILLED")
        journal.transition("v2-ETHUSDT-1", "OCO_INTENT")
        journal.transition("v2-ETHUSDT-1", "OCO_UNKNOWN")
        self.assertTrue(journal.has_pending())

    def test_duplicate_intent_is_rejected(self):
        journal = ExecutionJournal(self.path)
        journal.begin(client_order_id="v2-BTCUSDT-dup", symbol="BTCUSDT")
        with self.assertRaisesRegex(RuntimeError, "execution_intent_already_exists"):
            journal.begin(client_order_id="v2-BTCUSDT-dup", symbol="BTCUSDT")

    def test_invalid_stage_is_rejected(self):
        journal = ExecutionJournal(self.path)
        journal.begin(client_order_id="v2-BNBUSDT-1", symbol="BNBUSDT")
        with self.assertRaises(ValueError):
            journal.transition("v2-BNBUSDT-1", "MAYBE")

    def test_terminal_state_cannot_move_back_to_pending(self):
        journal = ExecutionJournal(self.path)
        journal.begin(client_order_id="v2-XRPUSDT-1", symbol="XRPUSDT")
        journal.transition("v2-XRPUSDT-1", "ABORTED")
        with self.assertRaisesRegex(RuntimeError, "execution_terminal_state_locked"):
            journal.transition("v2-XRPUSDT-1", "BUY_UNKNOWN")
        self.assertFalse(journal.has_pending())

    def test_invalid_jump_is_rejected(self):
        journal = ExecutionJournal(self.path)
        journal.begin(client_order_id="v2-ADAUSDT-1", symbol="ADAUSDT")
        with self.assertRaisesRegex(RuntimeError, "invalid_execution_transition"):
            journal.transition("v2-ADAUSDT-1", "PROTECTED")

    def test_details_are_merged_across_transitions(self):
        journal = ExecutionJournal(self.path)
        journal.begin(
            client_order_id="v2-BTCUSDT-meta",
            symbol="BTCUSDT",
            details={"quote_size": 10},
        )
        journal.transition(
            "v2-BTCUSDT-meta",
            "BUY_FILLED",
            details={"executed_qty": "0.00013"},
        )
        record = journal.get("v2-BTCUSDT-meta")
        self.assertEqual(record.details["quote_size"], 10)
        self.assertEqual(record.details["executed_qty"], "0.00013")


if __name__ == "__main__":
    unittest.main()
