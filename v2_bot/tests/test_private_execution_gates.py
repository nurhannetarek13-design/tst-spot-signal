import unittest

from v2_bot.config import Settings
from v2_bot.readiness import evaluate_live_readiness


class PrivateExecutionGateTests(unittest.TestCase):
    def test_credentials_are_only_present_when_both_values_exist(self):
        self.assertFalse(Settings(binance_api_key="", binance_api_secret="").private_credentials_present)
        self.assertFalse(Settings(binance_api_key="key", binance_api_secret="").private_credentials_present)
        self.assertTrue(Settings(binance_api_key="key", binance_api_secret="secret").private_credentials_present)

    def test_adapter_enable_without_credentials_fails_closed(self):
        settings = Settings(private_adapter_enabled=True)
        with self.assertRaisesRegex(RuntimeError, "V2_PRIVATE_ADAPTER_ENABLED"):
            settings.validate()

    def test_live_readiness_lists_every_unmet_private_gate(self):
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
            emergency_flatten_verified=False,
            paper_evidence_ready=False,
        )
        self.assertFalse(report.ready)
        self.assertIn("strategy_evidence_not_proven", report.blockers)
        self.assertIn("private_api_credentials_missing", report.blockers)
        self.assertIn("private_adapter_not_wired", report.blockers)
        self.assertIn("live_not_authorized", report.blockers)
        self.assertIn("live_engine_hard_lock_active", report.blockers)
        self.assertIn("emergency_flatten_not_verified", report.blockers)


if __name__ == "__main__":
    unittest.main()
