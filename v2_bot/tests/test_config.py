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


if __name__ == "__main__":
    unittest.main()
