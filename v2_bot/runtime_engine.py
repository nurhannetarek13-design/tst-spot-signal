from __future__ import annotations

from .binance_public import BinancePublicClient
from .config import Settings
from .engine import V2Engine
from .live_adapter import make_live_adapter
from .notifier import TelegramNotifier
from .readiness import evaluate_live_readiness, evaluate_paper_evidence
from .storage_backend import (
    make_execution_journal,
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
        self.execution_journal = make_execution_journal(settings)
        self.live_adapter = make_live_adapter(
            settings,
            journal=self.execution_journal,
        )
        self.persistence_proven = False
        self.notifier = TelegramNotifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
        )

    def close(self) -> None:
        super().close()
        if self.live_adapter is not None:
            self.live_adapter.close()

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
        paper_report = evaluate_paper_evidence(paper_stats)
        pending_execution_count = len(self.execution_journal.pending())
        live_report = evaluate_live_readiness(
            persistent_state_enabled=self.settings.persistent_state,
            persistence_proven=bool(self.persistence_proven),
            deploy_revision_present=bool(self.settings.deploy_revision),
            exchange_preflight_available=True,
            pending_execution_count=pending_execution_count,
            api_credentials_present=self.settings.private_credentials_present,
            private_adapter_wired=self.live_adapter is not None,
            explicit_live_authorization=self.settings.live_authorized,
            live_engine_lock_removed=self.settings.live_engine_unlock,
            emergency_flatten_verified=self.settings.emergency_flatten_verified,
            paper_evidence_ready=paper_report.ready,
        )
        summary["paper_stats"] = paper_stats
        summary["paper_evidence"] = paper_report.to_dict()
        summary["live_readiness"] = live_report.to_dict()
        summary["private_execution_gates"] = {
            "credentials_present": self.settings.private_credentials_present,
            "adapter_enabled": self.settings.private_adapter_enabled,
            "adapter_wired": self.live_adapter is not None,
            "explicit_live_authorization": self.settings.live_authorized,
            "engine_unlock": self.settings.live_engine_unlock,
            "emergency_flatten_verified": self.settings.emergency_flatten_verified,
            "pending_execution_count": pending_execution_count,
        }
        return summary
