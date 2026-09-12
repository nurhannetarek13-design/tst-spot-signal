from __future__ import annotations

from .binance_public import BinancePublicClient
from .config import Settings
from .engine import V2Engine
from .notifier import TelegramNotifier
from .storage_backend import make_shadow_outcome_ledger, make_state_store


class RuntimeV2Engine(V2Engine):
    """Production runtime wiring without changing the strategy engine itself."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.market = BinancePublicClient()
        self.state = make_state_store(settings)
        self.shadow_outcomes = make_shadow_outcome_ledger(settings)
        self.notifier = TelegramNotifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
        )
