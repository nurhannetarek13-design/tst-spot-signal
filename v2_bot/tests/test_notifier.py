import unittest
from unittest.mock import patch

import httpx

from v2_bot.notifier import TelegramNotifier


class NotifierTests(unittest.TestCase):
    def test_disabled_notifier_is_noop(self):
        notifier = TelegramNotifier("", "")
        self.assertFalse(notifier.send("hello"))

    @patch("v2_bot.notifier.httpx.post")
    def test_http_failure_does_not_crash_scanner(self, mock_post):
        mock_post.side_effect = httpx.TimeoutException("timeout")
        notifier = TelegramNotifier("token", "chat")
        self.assertFalse(notifier.send("hello"))


if __name__ == "__main__":
    unittest.main()
