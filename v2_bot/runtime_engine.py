from __future__ import annotations

from .binance_public import BinancePublicClient
from .config import Settings
from .engine import V2Engine
from .notifier import TelegramNotifier
from .readiness import evaluate_paper_evidence
from .storage_backend import (
    make_paper_evidence,
    make_shadow_outcome_ledger,
    make_state_store,
)


class RuntimeV2Engine(V2Engine):
    """Production runtime wiring without changing the strategy engine itself."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.market = BinancePublicClient()
        self.state = make_state_store(settings)
        self.shadow_outcomes = make_shadow_outcome_ledger(settings)
        self.paper_evidence = make_paper_evidence(settings)
        self.notifier = TelegramNotifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
        )

    def _execution_preflight(self, *, candidate, books):
        result = super()._execution_preflight(candidate=candidate, books=books)
        if self.settings.mode == "paper" and result.get("allowed"):
            claimed = self.state.claim_signal(
                symbol=candidate.symbol,
                signal_open_time=candidate.signal_open_time,
                kind="paper",
            )
            if not claimed:
                result = dict(result)
                result["allowed"] = False
                result["reasons"] = [
                    *list(result.get("reasons", [])),
                    "paper_signal_duplicate",
                ]
        return result

    def scan_once(self):
        summary = super().scan_once()
        paper_stats = self.paper_evidence.stats()
        summary["paper_stats"] = paper_stats
        summary["paper_evidence"] = evaluate_paper_evidence(paper_stats).to_dict()
        return summary
