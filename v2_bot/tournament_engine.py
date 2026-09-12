from __future__ import annotations

from typing import Any

from .risk import check_risk
from .runtime_engine import RuntimeV2Engine
from .shadow_tournament import (
    ShadowPromotionPolicy,
    StrategyShadowLedgerAdapter,
    decode_shadow_key,
    first_promotion_candidate,
    rank_shadow_strategies,
)
from .strategy_pool import STRATEGY_SPECS


class TournamentRuntimeV2Engine(RuntimeV2Engine):
    """Final V2 runtime: forward-test every strategy, then Paper the first proven winner.

    SHADOW is the permanent discovery lane. Every confirmed strategy signal is
    recorded independently with no real order. Once a strategy satisfies the
    frozen forward-evidence gate, its id is persisted and a single simulated
    Paper lane starts automatically. Live execution is never unlocked here.
    """

    PROMOTION_META_KEY = "shadow_tournament_paper_strategy_v1"

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.shadow_outcomes = StrategyShadowLedgerAdapter(self.shadow_outcomes)
        self.shadow_policy = ShadowPromotionPolicy.from_env()

    @property
    def _strategy_ids(self) -> set[str]:
        return {spec.strategy_id for spec in STRATEGY_SPECS}

    def _get_runtime_meta(self, key: str) -> str | None:
        getter = getattr(self.state, "get_meta", None)
        if callable(getter):
            return getter(key)

        store = getattr(self.state, "store", self.state)
        connector = getattr(store, "_connect", None)
        if not callable(connector):
            raise RuntimeError("runtime_meta_backend_unavailable")
        with connector() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM v2.runtime_meta WHERE key = %s", (key,))
            row = cur.fetchone()
        if not row:
            return None
        if isinstance(row, dict):
            return str(row["value"])
        return str(row[0])

    def _set_runtime_meta(self, key: str, value: str) -> None:
        setter = getattr(self.state, "set_meta", None)
        if callable(setter):
            setter(key, value)
            return

        store = getattr(self.state, "store", self.state)
        connector = getattr(store, "_connect", None)
        if not callable(connector):
            raise RuntimeError("runtime_meta_backend_unavailable")
        with connector() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO v2.runtime_meta (key, value, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = now()
                """,
                (key, value),
            )

    def _promoted_strategy_id(self) -> str | None:
        value = self._get_runtime_meta(self.PROMOTION_META_KEY)
        if value in self._strategy_ids:
            return value
        return None

    def _track_shadow_signal(self, *, candidate, books):
        ledger = getattr(self, "shadow_outcomes", None)
        if ledger is None:
            return {"tracked": False, "reason": "shadow_outcome_ledger_unavailable"}
        entry_price = float(books.get(candidate.symbol, {}).get("ask", 0.0)) or candidate.price
        take_profit_pct = (
            float(candidate.take_profit_pct)
            if candidate.take_profit_pct is not None
            else self.settings.take_profit_pct
        )
        stop_loss_pct = (
            float(candidate.stop_loss_pct)
            if candidate.stop_loss_pct is not None
            else self.settings.stop_loss_pct
        )
        strategy_id = str(getattr(candidate, "strategy_id", "strict_current"))
        try:
            outcome = ledger.open_signal(
                strategy_id=strategy_id,
                symbol=candidate.symbol,
                signal_open_time=candidate.signal_open_time,
                entry_price=entry_price,
                quote_size=self.settings.trade_size_usdt,
                take_profit_pct=take_profit_pct,
                stop_loss_pct=stop_loss_pct,
                fee_rate=self.settings.paper_fee_rate,
            )
            return {
                "tracked": True,
                "strategy_id": strategy_id,
                "entry_price": outcome.entry_price,
                "take_profit": outcome.take_profit,
                "stop_loss": outcome.stop_loss,
            }
        except Exception as exc:
            return {
                "tracked": False,
                "strategy_id": strategy_id,
                "reason": str(exc),
                "error_type": type(exc).__name__,
            }

    def _manage_shadow_outcomes(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if self.settings.mode != "shadow":
            return events
        ledger = getattr(self, "shadow_outcomes", None)
        if ledger is None:
            return events

        for outcome in ledger.open_outcomes():
            market_symbol, strategy_id = decode_shadow_key(outcome.symbol)
            try:
                candles = self.market.klines(market_symbol, "15m", 8)
                updated = outcome
                for candle in sorted(candles, key=lambda row: float(row.get("open_time", 0) or 0)):
                    updated = ledger.evaluate_closed_candle(updated, candle)
                    if updated.status != "OPEN":
                        events.append(
                            {
                                "event": "shadow_outcome_closed",
                                "symbol": market_symbol,
                                "strategy_id": strategy_id,
                                "signal_open_time": updated.signal_open_time,
                                "status": updated.status,
                                "reason": updated.reason,
                                "exit_price": updated.exit_price,
                                "pnl_usdt_net_fees": updated.pnl_usdt,
                            }
                        )
                        break
            except Exception as exc:
                events.append(
                    {
                        "event": "shadow_outcome_error",
                        "symbol": market_symbol,
                        "strategy_id": strategy_id,
                        "signal_open_time": outcome.signal_open_time,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
        return events

    def _manage_paper_exits(self, books: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
        if self.settings.mode == "paper":
            return super()._manage_paper_exits(books)
        if self.settings.mode != "shadow" or self._promoted_strategy_id() is None:
            return []

        events: list[dict[str, Any]] = []
        for position in self.state.list_open_positions():
            book = books.get(position.symbol)
            if not book:
                continue
            bid = float(book.get("bid", 0.0) or 0.0)
            if bid <= 0:
                continue
            reason = ""
            if bid >= position.take_profit:
                reason = "take_profit"
            elif bid <= position.stop_loss:
                reason = "stop_loss"
            if not reason:
                continue
            strategy_id = self.state.strategy_for_position(position) or self._promoted_strategy_id()
            pnl = self.state.close_position(
                position,
                exit_price=bid,
                reason=reason,
                fee_rate=self.settings.paper_fee_rate,
            )
            event = {
                "event": "auto_paper_close",
                "symbol": position.symbol,
                "strategy_id": strategy_id,
                "exit_price": bid,
                "reason": reason,
                "pnl_usdt_net_fees": round(pnl, 6),
            }
            events.append(event)
            self.notifier.send(
                f"V2 AUTO PAPER CLOSE {position.symbol}\n"
                f"Strategy: {strategy_id}\n"
                f"Reason: {reason}\n"
                f"PnL net fees: {pnl:+.4f} USDT"
            )
        return events

    def _track_all_shadow_signals(self, summary: dict[str, Any]) -> list[dict[str, Any]]:
        if self.settings.mode != "shadow":
            return []

        books = self.market.book_tickers()
        flattened = [
            candidate
            for pool in self._strategy_candidates.values()
            for candidate in pool
        ]
        confirmed = [candidate for candidate in flattened if candidate.strategy_signal_ok]
        confirmed.sort(
            key=lambda item: (item.score, item.relative_volume, item.taker_buy_ratio),
            reverse=True,
        )

        base_action = summary.get("action") or {}
        base_key = None
        if base_action.get("event") == "shadow_signal":
            base_key = (
                str(base_action.get("symbol", "")),
                float(base_action.get("signal_open_time", 0.0) or 0.0),
                str(base_action.get("strategy_id", "")),
            )

        tracked: list[dict[str, Any]] = []
        for candidate in confirmed:
            kind = f"shadow_tournament:{candidate.strategy_id}"
            key = (candidate.symbol, float(candidate.signal_open_time), candidate.strategy_id)
            claimed = self.state.claim_signal(
                symbol=candidate.symbol,
                signal_open_time=candidate.signal_open_time,
                kind=kind,
            )
            if not claimed:
                continue

            if base_key == key:
                tracked.append(
                    {
                        "symbol": candidate.symbol,
                        "strategy_id": candidate.strategy_id,
                        "signal_open_time": candidate.signal_open_time,
                        "tracked": True,
                        "source": "base_shadow_action",
                    }
                )
                continue

            preflight = self._execution_preflight(candidate=candidate, books=books)
            if not preflight.get("allowed"):
                tracked.append(
                    {
                        "symbol": candidate.symbol,
                        "strategy_id": candidate.strategy_id,
                        "signal_open_time": candidate.signal_open_time,
                        "tracked": False,
                        "reason": "exchange_preflight_failed",
                        "preflight_reasons": list(preflight.get("reasons", [])),
                    }
                )
                continue

            result = self._track_shadow_signal(candidate=candidate, books=books)
            tracked.append(
                {
                    "symbol": candidate.symbol,
                    "strategy_id": candidate.strategy_id,
                    "signal_open_time": candidate.signal_open_time,
                    **result,
                }
            )
        return tracked

    def _rank_and_promote(self) -> tuple[list[dict[str, Any]], str | None, bool]:
        stats_by_strategy = self.shadow_outcomes.stats_by_strategy()
        rankings = rank_shadow_strategies(
            stats_by_strategy,
            self.shadow_policy,
            allowed_strategy_ids=self._strategy_ids,
        )
        promoted = self._promoted_strategy_id()
        promoted_now = False
        if promoted is None:
            candidate = first_promotion_candidate(rankings)
            if candidate is not None:
                self._set_runtime_meta(self.PROMOTION_META_KEY, candidate)
                promoted = candidate
                promoted_now = True
                self.notifier.send(
                    "V2 SHADOW -> PAPER PROMOTION\n"
                    f"Strategy: {candidate}\n"
                    "Forward gate passed. Starting simulated Paper lane only.\n"
                    "Live remains HARD-LOCKED."
                )
        return rankings, promoted, promoted_now

    def _auto_paper_open(
        self,
        *,
        promoted_strategy_id: str | None,
        paper_events: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if self.settings.mode != "shadow" or promoted_strategy_id is None:
            return None
        if not self.settings.persistent_state or not bool(self.persistence_proven):
            return {
                "event": "auto_paper_blocked",
                "strategy_id": promoted_strategy_id,
                "reason": "persistent_state_not_proven",
            }
        if self.settings.live_trading:
            return {
                "event": "auto_paper_blocked",
                "strategy_id": promoted_strategy_id,
                "reason": "live_flag_must_remain_off_in_tournament_mode",
            }

        candidates = [
            candidate
            for pool in self._strategy_candidates.values()
            for candidate in pool
            if candidate.strategy_id == promoted_strategy_id and candidate.strategy_signal_ok
        ]
        candidates.sort(
            key=lambda item: (item.score, item.relative_volume, item.taker_buy_ratio),
            reverse=True,
        )
        if not candidates:
            return None

        books = self.market.book_tickers()
        open_positions = self.state.list_open_positions()
        open_symbols = {position.symbol for position in open_positions}
        just_closed = {event.get("symbol") for event in paper_events}
        actionable = [
            candidate
            for candidate in candidates
            if candidate.symbol not in open_symbols and candidate.symbol not in just_closed
        ]
        if not actionable:
            return None

        best = actionable[0]
        decision = check_risk(
            settings=self.settings,
            candidate=best,
            realized_pnl_today=self.state.realized_pnl_today(),
            open_positions=len(open_positions),
        )
        if not decision.allowed:
            return {
                "event": "auto_paper_blocked",
                "strategy_id": promoted_strategy_id,
                "symbol": best.symbol,
                "reason": decision.reason,
            }

        preflight = self._execution_preflight(candidate=best, books=books)
        if not preflight.get("allowed"):
            return {
                "event": "auto_paper_blocked",
                "strategy_id": promoted_strategy_id,
                "symbol": best.symbol,
                "reason": "exchange_preflight_failed",
                "preflight_reasons": list(preflight.get("reasons", [])),
            }

        claimed = self.state.claim_signal(
            symbol=best.symbol,
            signal_open_time=best.signal_open_time,
            kind=f"auto_paper:{promoted_strategy_id}",
        )
        if not claimed:
            return None

        self.state.prepare_profile(
            symbol=best.symbol,
            strategy_id=promoted_strategy_id,
            take_profit_pct=float(best.take_profit_pct),
            stop_loss_pct=float(best.stop_loss_pct),
        )
        entry_price = float(books.get(best.symbol, {}).get("ask", 0.0)) or best.price
        try:
            position = self.state.open_position(
                symbol=best.symbol,
                entry_price=entry_price,
                quote_size=self.settings.trade_size_usdt,
                take_profit_pct=float(best.take_profit_pct),
                stop_loss_pct=float(best.stop_loss_pct),
            )
        except Exception:
            self.state.clear_profile(best.symbol)
            raise

        self.notifier.send(
            f"V2 AUTO PAPER BUY {position.symbol}\n"
            f"Strategy: {promoted_strategy_id}\n"
            f"Score: {best.score}/100\n"
            f"Entry: {position.entry_price:.8f}\n"
            f"TP: {position.take_profit:.8f}\n"
            f"SL: {position.stop_loss:.8f}\n"
            "Live: OFF"
        )
        return {
            "event": "auto_paper_open",
            "strategy_id": promoted_strategy_id,
            "symbol": position.symbol,
            "score": best.score,
            "entry_price": position.entry_price,
            "quote_size": position.quote_size,
            "take_profit": position.take_profit,
            "stop_loss": position.stop_loss,
        }

    def scan_once(self):
        summary = super().scan_once()
        tracked = self._track_all_shadow_signals(summary)
        rankings, promoted, promoted_now = self._rank_and_promote()
        paper_events = list(summary.get("paper_events") or [])
        auto_paper_action = self._auto_paper_open(
            promoted_strategy_id=promoted,
            paper_events=paper_events,
        )

        paper_by_strategy = self.paper_evidence.stats_by_strategy()
        summary["shadow_tournament"] = {
            "enabled": self.settings.mode == "shadow",
            "live_trading": False,
            "policy": self.shadow_policy.to_dict(),
            "strategies": len(self._strategy_ids),
            "new_signal_records": tracked,
            "rankings": rankings,
            "promoted_strategy_id": promoted,
            "promoted_this_cycle": promoted_now,
            "promotion_is_paper_only": True,
        }
        summary["auto_paper_action"] = auto_paper_action
        summary["auto_paper_stats"] = (
            paper_by_strategy.get(promoted, {}) if promoted is not None else {}
        )
        summary["live_trading"] = False
        return summary
