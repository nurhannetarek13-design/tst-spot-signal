from __future__ import annotations

import httpx


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, timeout: float = 10.0) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            response = httpx.post(
                url,
                json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            # Notifications are secondary. A Telegram outage must never stop
            # market scanning or paper-state management.
            return False
        return True
