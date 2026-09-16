import unittest

from v2_bot.config import Settings
from v2_bot.live_adapter import make_live_adapter


class _Journal:
    pass


class LiveAdapterTests(unittest.TestCase):
    def test_paper_never_builds_private_adapter(self):
        settings = Settings(
            mode="paper",
            private_adapter_enabled=True,
            binance_api_key="key",
            binance_api_secret="secret",
        )
        self.assertIsNone(make_live_adapter(settings, journal=_Journal()))

    def test_live_adapter_disabled_returns_none(self):
        settings = Settings(
            mode="live",
            live_trading=True,
            private_adapter_enabled=False,
            binance_api_key="key",
            binance_api_secret="secret",
        )
        self.assertIsNone(make_live_adapter(settings, journal=_Journal()))

    def test_live_adapter_builds_without_network_request_when_deliberately_enabled(self):
        settings = Settings(
            mode="live",
            live_trading=True,
            private_adapter_enabled=True,
            binance_api_key="key",
            binance_api_secret="secret",
        )
        bundle = make_live_adapter(settings, journal=_Journal())
        self.assertIsNotNone(bundle)
        self.assertIsNotNone(bundle.executor)
        self.assertIsNotNone(bundle.reconciler)
        bundle.close()


if __name__ == "__main__":
    unittest.main()
