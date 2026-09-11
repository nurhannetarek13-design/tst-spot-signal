import unittest

from v2_bot.readiness import evaluate_live_readiness, evaluate_paper_readiness


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
        )
        self.assertFalse(report.ready)
        self.assertIn("private_api_credentials_missing", report.blockers)
        self.assertIn("private_adapter_not_wired", report.blockers)
        self.assertIn("live_not_authorized", report.blockers)
        self.assertIn("live_engine_hard_lock_active", report.blockers)

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
        )
        self.assertFalse(report.ready)
        self.assertIn("invalid_pending_execution_count", report.blockers)


if __name__ == "__main__":
    unittest.main()
