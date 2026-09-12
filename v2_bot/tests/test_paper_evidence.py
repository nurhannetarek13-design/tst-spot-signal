import tempfile
import unittest
from pathlib import Path

from v2_bot.config import Settings
from v2_bot.paper_evidence import SqlitePaperEvidence, summarize_pnls
from v2_bot.readiness import evaluate_live_readiness, evaluate_paper_evidence
from v2_bot.state import StateStore


class PaperEvidenceTests(unittest.TestCase):
    def test_summary_reports_profit_factor_expectancy_and_drawdown(self):
        stats = summarize_pnls([1.0, -0.5, -0.75, 2.0])
        self.assertEqual(stats["closed"], 4)
        self.assertEqual(stats["wins"], 2)
        self.assertEqual(stats["losses"], 2)
        self.assertEqual(stats["win_rate"], 0.5)
        self.assertEqual(stats["profit_factor"], 2.4)
        self.assertEqual(stats["expectancy_usdt"], 0.4375)
        self.assertEqual(stats["max_drawdown_usdt"], 1.25)
        self.assertEqual(stats["max_consecutive_losses"], 2)

    def test_no_losses_keeps_profit_factor_unproven(self):
        stats = summarize_pnls([0.1, 0.2])
        self.assertIsNone(stats["profit_factor"])

    def test_sqlite_evidence_reads_real_trade_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.sqlite3")
            store = StateStore(path)
            first = store.open_position(
                symbol="BTCUSDT",
                entry_price=100.0,
                quote_size=10.0,
                take_profit_pct=0.01,
                stop_loss_pct=0.01,
            )
            store.close_position(first, exit_price=101.0, reason="tp", fee_rate=0.0)
            second = store.open_position(
                symbol="ETHUSDT",
                entry_price=100.0,
                quote_size=10.0,
                take_profit_pct=0.01,
                stop_loss_pct=0.01,
            )
            store.close_position(second, exit_price=99.0, reason="sl", fee_rate=0.0)
            stats = SqlitePaperEvidence(path).stats()
            self.assertEqual(stats["closed"], 2)
            self.assertEqual(stats["wins"], 1)
            self.assertEqual(stats["losses"], 1)
            self.assertEqual(stats["profit_factor"], 1.0)

    def test_paper_gate_requires_sample_pf_and_positive_expectancy(self):
        failing = evaluate_paper_evidence(
            {"closed": 10, "profit_factor": 1.8, "expectancy_usdt": 0.02}
        )
        self.assertFalse(failing.ready)
        self.assertIn("paper_sample_insufficient", failing.blockers)

        passing = evaluate_paper_evidence(
            {"closed": 60, "profit_factor": 1.25, "expectancy_usdt": 0.01}
        )
        self.assertTrue(passing.ready)

    def test_paper_evidence_can_satisfy_live_strategy_evidence_slot(self):
        report = evaluate_live_readiness(
            persistent_state_enabled=True,
            persistence_proven=True,
            deploy_revision_present=True,
            exchange_preflight_available=True,
            pending_execution_count=0,
            api_credentials_present=True,
            private_adapter_wired=True,
            explicit_live_authorization=True,
            live_engine_lock_removed=True,
            emergency_flatten_verified=True,
            strategy_evidence_ready=False,
            paper_evidence_ready=True,
        )
        self.assertTrue(report.ready)


if __name__ == "__main__":
    unittest.main()
