import unittest

from v2_bot.readiness import (
    evaluate_live_readiness,
    evaluate_paper_readiness,
    evaluate_strategy_evidence,
)


class ReadinessTests(unittest.TestCase):
    def test_current_runtime_is_not_paper_ready_without_durable_state(self):
        report = evaluate_paper_readiness(
            persistent_state_enabled=False,
            persistence_proven=False,
            deploy_revision_present=True,
            exchange_preflight_available=True,
        )
        self.assertFalse(report.ready)
        self.assertIn("persistent_state_disabled", report.blockers)
        self.assertIn("persistent_storage_not_proven", report.blockers)

    def test_paper_ready_only_after_cross_deploy_persistence_proof(self):
        report = evaluate_paper_readiness(
            persistent_state_enabled=True,
            persistence_proven=True,
            deploy_revision_present=True,
            exchange_preflight_available=True,
        )
        self.assertTrue(report.ready)
        self.assertEqual(report.blockers, ())

    def test_paper_does_not_require_strategy_profitability_evidence(self):
        paper = evaluate_paper_readiness(
            persistent_state_enabled=True,
            persistence_proven=True,
            deploy_revision_present=True,
            exchange_preflight_available=True,
        )
        evidence = evaluate_strategy_evidence({})
        self.assertTrue(paper.ready)
        self.assertFalse(evidence.ready)

    def test_strategy_evidence_requires_sample_pf_expectancy_and_low_ambiguity(self):
        report = evaluate_strategy_evidence(
            {
                "decisive": 20,
                "profit_factor": 0.9,
                "expectancy_usdt": -0.01,
                "ambiguous_rate": 0.25,
            }
        )
        self.assertFalse(report.ready)
        self.assertEqual(
            report.blockers,
            (
                "shadow_sample_insufficient",
                "shadow_profit_factor_unproven",
                "shadow_expectancy_nonpositive",
                "shadow_ambiguity_too_high",
            ),
        )

    def test_strategy_evidence_can_pass_conservative_promotion_gate(self):
        report = evaluate_strategy_evidence(
            {
                "decisive": 80,
                "profit_factor": 1.35,
                "expectancy_usdt": 0.012,
                "ambiguous_rate": 0.05,
            }
        )
        self.assertTrue(report.ready)
        self.assertEqual(report.blockers, ())

    def test_missing_strategy_metrics_fail_closed(self):
        report = evaluate_strategy_evidence({})
        self.assertFalse(report.ready)
        self.assertIn("shadow_sample_insufficient", report.blockers)
        self.assertIn("shadow_profit_factor_unproven", report.blockers)
        self.assertIn("shadow_expectancy_nonpositive", report.blockers)
        self.assertIn("shadow_ambiguity_too_high", report.blockers)

    def test_all_wins_without_observed_losses_does_not_prove_profit_factor(self):
        report = evaluate_strategy_evidence(
            {
                "decisive": 80,
                "profit_factor": None,
                "expectancy_usdt": 0.05,
                "ambiguous_rate": 0.0,
            }
        )
        self.assertFalse(report.ready)
        self.assertEqual(report.blockers, ("shadow_profit_factor_unproven",))

    def test_live_remains_blocked_even_if_storage_is_ready(self):
        report = evaluate_live_readiness(
            persistent_state_enabled=True,
            persistence_proven=True,
            deploy_revision_present=True,
            exchange_preflight_available=True,
            pending_execution_count=0,
            api_credentials_present=False,
            private_adapter_wired=False,
            explicit_live_authorization=False,
            live_engine_lock_removed=False,
            emergency_flatten_verified=True,
            strategy_evidence_ready=True,
        )
        self.assertFalse(report.ready)
        self.assertIn("private_api_credentials_missing", report.blockers)
        self.assertIn("private_adapter_not_wired", report.blockers)
        self.assertIn("live_not_authorized", report.blockers)
        self.assertIn("live_engine_hard_lock_active", report.blockers)

    def test_live_is_blocked_when_strategy_evidence_is_not_proven(self):
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
        )
        self.assertFalse(report.ready)
        self.assertEqual(report.blockers, ("strategy_evidence_not_proven",))

    def test_pending_execution_alone_blocks_otherwise_ready_live_state(self):
        report = evaluate_live_readiness(
            persistent_state_enabled=True,
            persistence_proven=True,
            deploy_revision_present=True,
            exchange_preflight_available=True,
            pending_execution_count=1,
            api_credentials_present=True,
            private_adapter_wired=True,
            explicit_live_authorization=True,
            live_engine_lock_removed=True,
            emergency_flatten_verified=True,
            strategy_evidence_ready=True,
        )
        self.assertFalse(report.ready)
        self.assertEqual(report.blockers, ("pending_execution_recovery_required",))

    def test_all_independent_live_gates_must_be_true(self):
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
            strategy_evidence_ready=True,
        )
        self.assertTrue(report.ready)
        self.assertEqual(report.blockers, ())

    def test_invalid_pending_count_fails_closed(self):
        report = evaluate_live_readiness(
            persistent_state_enabled=True,
            persistence_proven=True,
            deploy_revision_present=True,
            exchange_preflight_available=True,
            pending_execution_count=-1,
            api_credentials_present=True,
            private_adapter_wired=True,
            explicit_live_authorization=True,
            live_engine_lock_removed=True,
            emergency_flatten_verified=True,
            strategy_evidence_ready=True,
        )
        self.assertFalse(report.ready)
        self.assertIn("invalid_pending_execution_count", report.blockers)


if __name__ == "__main__":
    unittest.main()
