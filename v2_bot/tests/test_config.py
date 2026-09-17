import unittest

from v2_bot.config import Settings


class ConfigTests(unittest.TestCase):
    def test_default_settings_validate(self):
        Settings().validate()

    def test_rejects_invalid_score(self):
        with self.assertRaisesRegex(ValueError, "V2_MIN_SCORE"):
            Settings(min_score=101).validate()

    def test_rejects_zero_open_positions_limit(self):
        with self.assertRaisesRegex(ValueError, "V2_MAX_OPEN_POSITIONS"):
            Settings(max_open_positions=0).validate()

    def test_live_mode_requires_explicit_live_flag(self):
        with self.assertRaisesRegex(RuntimeError, "V2_LIVE_TRADING=false"):
            Settings(mode="live", live_trading=False).validate()

    def test_paper_mode_requires_persistent_state(self):
        with self.assertRaisesRegex(RuntimeError, "V2_PERSISTENT_STATE=true"):
            Settings(mode="paper", persistent_state=False).validate()

    def test_paper_mode_requires_deploy_revision(self):
        with self.assertRaisesRegex(RuntimeError, "V2_DEPLOY_REV"):
            Settings(mode="paper", persistent_state=True, deploy_revision="").validate()

    def test_paper_mode_allows_explicit_persistent_state_and_revision(self):
        Settings(mode="paper", persistent_state=True, deploy_revision="rev-a").validate()

    def test_live_mode_with_live_flag_still_requires_persistent_state(self):
        with self.assertRaisesRegex(RuntimeError, "V2_PERSISTENT_STATE=true"):
            Settings(mode="live", live_trading=True, persistent_state=False).validate()

    def test_live_mode_requires_deploy_revision_after_live_and_persistence_flags(self):
        with self.assertRaisesRegex(RuntimeError, "V2_DEPLOY_REV"):
            Settings(
                mode="live",
                live_trading=True,
                persistent_state=True,
                deploy_revision="",
            ).validate()


if __name__ == "__main__":
    unittest.main()
