from __future__ import annotations

from collections import Counter

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
from .strategy_pool import evaluate_pool, registry_snapshot


class RuntimeV2Engine(V2Engine):
    """Production runtime wiring plus the isolated multi-strategy router."""

    PAPER_READY_EVENT_SYMBOL = "__V2_SYSTEM__"
    PAPER_READY_EVENT_KIND = "paper_evidence_ready_v1"

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
        self._strategy_candidates: dict[str, list] = {}

    def close(self) -> None:
        super().close()
        if self.live_adapter is not None:
            self.live_adapter.close()

    def _evaluate_symbol(self, *, symbol, quote_volume_24h, books, btc_1h):
        book = books.get(symbol, {})
        pool = evaluate_pool(
            symbol=symbol,
            candles_15m=self.market.klines(symbol, "15m", 120),
            candles_1h=self.market.klines(symbol, "1h", 120),
            candles_4h=self.market.klines(symbol, "4h", 120),
            btc_1h=btc_1h,
            spread_bps=self._spread_bps(book),
            quote_volume_24h=quote_volume_24h,
            settings=self.settings,
            mode=self.settings.mode,
        )
        if not pool:
            raise RuntimeError("strategy_pool_empty")
        self._strategy_candidates[symbol] = pool
        return pool[0]

    @staticmethod
    def _failed_gates(candidate, settings):
        if getattr(candidate, "strategy_status", "LEGACY") != "LEGACY":
            return list(getattr(candidate, "strategy_failed_gates", ()) or ())
        return V2Engine._failed_gates(candidate, settings)

    @classmethod
    def _gate_failure_counts(cls, candidates, settings):
        counts: Counter[str] = Counter()
        for candidate in candidates:
            counts.update(cls._failed_gates(candidate, settings))
        return dict(sorted(counts.items()))

    @staticmethod
    def _is_breakout_prealert(candidate, settings):
        if getattr(candidate, "strategy_status", "LEGACY") != "LEGACY":
            return False
        return V2Engine._is_breakout_prealert(candidate, settings)

    def _execution_preflight(self, *, candidate, books):
        result = super()._execution_preflight(candidate=candidate, books=books)
        if self.settings.mode == "paper" and result.get("allowed"):
            strategy_id = getattr(candidate, "strategy_id", "strict_current")
            claimed = self.state.claim_signal(
                symbol=candidate.symbol,
                signal_open_time=candidate.signal_open_time,
                kind=f"paper:{strategy_id}",
            )
            if not claimed:
                result = dict(result)
                result["allowed"] = False
                result["reasons"] = [
                    *list(result.get("reasons", [])),
                    "paper_signal_duplicate",
                ]
        return result

    @staticmethod
    def _metric(value, *, percent: bool = False) -> str:
        if value is None:
            return "unproven"
        number = float(value)
        if percent:
            return f"{number:.1%}"
        return f"{number:.4f}"

    def _notify_paper_stats(self, paper_stats) -> None:
        self.notifier.send(
            "V2 PAPER CUMULATIVE\n"
            f"Closed: {paper_stats['closed']}\n"
            f"Win rate: {self._metric(paper_stats['win_rate'], percent=True)}\n"
            f"Net PnL: {self._metric(paper_stats['net_pnl_usdt'])} USDT\n"
            f"Expectancy: {self._metric(paper_stats['expectancy_usdt'])} USDT/trade\n"
            f"Profit factor: {self._metric(paper_stats['profit_factor'])}\n"
            f"Max DD: {self._metric(paper_stats['max_drawdown_usdt'])} USDT"
        )

    def _maybe_notify_paper_ready(self, paper_stats, paper_report) -> bool:
        """Send a one-time promotion-review alert when real Paper evidence passes.

        This never changes mode, credentials, adapter flags, or the Live hard
        lock. It only tells the operator the evidence gate is ready for review.
        """
        if self.settings.mode != "paper" or not paper_report.ready:
            return False

        already_claimed = not self.state.claim_signal(
            symbol=self.PAPER_READY_EVENT_SYMBOL,
            signal_open_time=0.0,
            kind=self.PAPER_READY_EVENT_KIND,
        )
        if already_claimed:
            return False

        self.notifier.send(
            "V2 PAPER EVIDENCE READY — REVIEW ONLY\n"
            f"Closed: {paper_stats['closed']}\n"
            f"Win rate: {self._metric(paper_stats['win_rate'], percent=True)}\n"
            f"Net PnL: {self._metric(paper_stats['net_pnl_usdt'])} USDT\n"
            f"Expectancy: {self._metric(paper_stats['expectancy_usdt'])} USDT/trade\n"
            f"Profit factor: {self._metric(paper_stats['profit_factor'])}\n"
            f"Max DD: {self._metric(paper_stats['max_drawdown_usdt'])} USDT\n"
            "Live remains OFF. Manual review/authorization is still required."
        )
        return True

    def scan_once(self):
        self._strategy_candidates = {}
        summary = super().scan_once()

        flattened = [
            candidate
            for pool in self._strategy_candidates.values()
            for candidate in pool
        ]
        signal_candidates = [c for c in flattened if c.strategy_signal_ok]
        executable_candidates = [c for c in flattened if c.eligible]
        signal_candidates.sort(
            key=lambda c: (c.score, c.relative_volume, c.taker_buy_ratio),
            reverse=True,
        )
        executable_candidates.sort(
            key=lambda c: (c.score, c.relative_volume, c.taker_buy_ratio),
            reverse=True,
        )
        registry = registry_snapshot()
        summary["strategy_router"] = {
            "registered": len(registry),
            "status_counts": dict(Counter(item["status"] for item in registry)),
            "evaluations": len(flattened),
            "signals": len(signal_candidates),
            "executable_signals": len(executable_candidates),
            "top_signals": [c.to_dict() for c in signal_candidates[:8]],
            "top_executable": [c.to_dict() for c in executable_candidates[:5]],
        }
        summary["strategy_registry"] = [
            {
                "strategy_id": item["strategy_id"],
                "family": item["family"],
                "status": item["status"],
            }
            for item in registry
        ]

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
        if self.settings.mode == "paper" and summary.get("paper_events"):
            self._notify_paper_stats(paper_stats)
        summary["paper_ready_alert_sent"] = self._maybe_notify_paper_ready(
            paper_stats,
            paper_report,
        )
        return summary
