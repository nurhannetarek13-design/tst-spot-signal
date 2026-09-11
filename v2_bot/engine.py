from __future__ import annotations

import json
from typing import Any

from .binance_public import BinancePublicClient
from .config import Settings
from .notifier import TelegramNotifier
from .risk import check_risk
from .state import StateStore
from .strategy import Candidate, evaluate_candidate


LEVERAGED_TOKEN_MARKERS = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
STABLE_OR_FIAT_BASE_ASSETS = {
    "USDC",
    "FDUSD",
    "TUSD",
    "USDP",
    "DAI",
    "BUSD",
    "USDE",
    "USD1",
    "PYUSD",
    "EUR",
    "AEUR",
    "EURI",
    "GBP",
    "AUD",
    "BRL",
    "TRY",
}
GATE_NAMES = (
    "btc_regime",
    "trend_15m",
    "trend_1h",
    "trend_4h",
    "entry_setup",
    "relative_volume",
    "taker_flow",
    "spread",
    "score",
)


class V2Engine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.market = BinancePublicClient()
        self.state = StateStore(settings.state_db)
        self.notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)

    def close(self) -> None:
        self.market.close()

    @staticmethod
    def _is_plain_spot_symbol(symbol: str) -> bool:
        """Keep the scanner on conventional Binance symbols only.

        Binance can occasionally surface promotional or newly-created symbols
        whose base asset contains non-ASCII characters. Those pairs may have
        incomplete candle history and are outside V2's intended research
        universe. Digits remain allowed so assets such as 1000PEPE are kept.
        """
        return bool(symbol) and symbol.isascii() and symbol.isalnum() and symbol == symbol.upper()

    def _build_universe(self, tickers: list[dict[str, Any]]) -> list[tuple[str, float]]:
        ranked: list[tuple[str, float]] = []
        quote = self.settings.quote_asset
        for row in tickers:
            symbol = str(row.get("symbol", ""))
            if not self._is_plain_spot_symbol(symbol):
                continue
            if not symbol.endswith(quote):
                continue
            if any(symbol.endswith(marker) for marker in LEVERAGED_TOKEN_MARKERS):
                continue
            base_asset = symbol[: -len(quote)] if quote else ""
            if not base_asset or base_asset in STABLE_OR_FIAT_BASE_ASSETS:
                continue
            try:
                quote_volume = float(row.get("quoteVolume", 0.0))
            except (TypeError, ValueError):
                continue
            if quote_volume < self.settings.min_quote_volume_24h:
                continue
            ranked.append((symbol, quote_volume))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked[: self.settings.universe_limit]

    @staticmethod
    def _spread_bps(book: dict[str, float]) -> float:
        bid = book.get("bid", 0.0)
        ask = book.get("ask", 0.0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return float("inf")
        mid = (bid + ask) / 2.0
        return ((ask - bid) / mid) * 10_000.0

    @staticmethod
    def _failed_gates(candidate: Candidate, settings: Settings) -> list[str]:
        failed: list[str] = []
        if not candidate.btc_regime_ok:
            failed.append("btc_regime")
        if not candidate.trend_15m:
            failed.append("trend_15m")
        if not candidate.trend_1h:
            failed.append("trend_1h")
        if not candidate.trend_4h:
            failed.append("trend_4h")
        if not (candidate.breakout_retest or candidate.pullback):
            failed.append("entry_setup")
        if not candidate.rel_volume_ok:
            failed.append("relative_volume")
        if not candidate.taker_flow_ok:
            failed.append("taker_flow")
        if candidate.spread_bps > settings.max_spread_bps:
            failed.append("spread")
        if candidate.score < settings.min_score:
            failed.append("score")
        return failed

    @classmethod
    def _gate_failure_counts(cls, candidates: list[Candidate], settings: Settings) -> dict[str, int]:
        counts = {name: 0 for name in GATE_NAMES}
        for candidate in candidates:
            for gate in cls._failed_gates(candidate, settings):
                counts[gate] += 1
        return counts

    def _manage_paper_exits(self, books: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if self.settings.mode != "paper":
            return events
        for position in self.state.list_open_positions():
            book = books.get(position.symbol)
            if not book:
                continue
            bid = book.get("bid", 0.0)
            if bid <= 0:
                continue
            reason = ""
            if bid >= position.take_profit:
                reason = "take_profit"
            elif bid <= position.stop_loss:
                reason = "stop_loss"
            if not reason:
                continue
            pnl = self.state.close_position(
                position,
                exit_price=bid,
                reason=reason,
                fee_rate=self.settings.paper_fee_rate,
            )
            event = {
                "event": "paper_close",
                "symbol": position.symbol,
                "exit_price": bid,
                "reason": reason,
                "pnl_usdt_net_fees": round(pnl, 6),
            }
            events.append(event)
            self.notifier.send(
                f"V2 PAPER CLOSE {position.symbol}\n"
                f"Reason: {reason}\n"
                f"PnL net fees: {pnl:+.4f} USDT"
            )
        return events

    def _evaluate_symbol(
        self,
        *,
        symbol: str,
        quote_volume_24h: float,
        books: dict[str, dict[str, float]],
        btc_1h: list[dict[str, float]],
    ) -> Candidate:
        book = books.get(symbol, {})
        return evaluate_candidate(
            symbol=symbol,
            candles_15m=self.market.klines(symbol, "15m", 120),
            candles_1h=self.market.klines(symbol, "1h", 120),
            candles_4h=self.market.klines(symbol, "4h", 120),
            btc_1h=btc_1h,
            spread_bps=self._spread_bps(book),
            quote_volume_24h=quote_volume_24h,
            min_quote_volume_24h=self.settings.min_quote_volume_24h,
            max_spread_bps=self.settings.max_spread_bps,
            min_score=self.settings.min_score,
        )

    def scan_once(self) -> dict[str, Any]:
        tickers = self.market.ticker_24h()
        books = self.market.book_tickers()
        paper_events = self._manage_paper_exits(books)
        closed_symbols_this_cycle = {event["symbol"] for event in paper_events}
        universe = self._build_universe(tickers)
        btc_1h = self.market.klines("BTCUSDT", "1h", 120)

        candidates: list[Candidate] = []
        errors: list[dict[str, str]] = []
        for symbol, quote_volume in universe:
            try:
                candidate = self._evaluate_symbol(
                    symbol=symbol,
                    quote_volume_24h=quote_volume,
                    books=books,
                    btc_1h=btc_1h,
                )
                candidates.append(candidate)
            except Exception as exc:  # isolate one bad symbol from the cycle
                errors.append({"symbol": symbol, "error": str(exc)})

        candidates.sort(key=lambda c: (c.score, c.relative_volume), reverse=True)
        eligible = [c for c in candidates if c.eligible]
        gate_failure_counts = self._gate_failure_counts(candidates, self.settings)
        top_near_miss = None
        for candidate in candidates:
            failed_gates = self._failed_gates(candidate, self.settings)
            if failed_gates:
                top_near_miss = {
                    "symbol": candidate.symbol,
                    "score": candidate.score,
                    "entry_setup": candidate.entry_setup,
                    "failed_gates": failed_gates,
                }
                break

        action: dict[str, Any] | None = None

        if eligible:
            open_positions = self.state.list_open_positions()
            open_symbols = {p.symbol for p in open_positions}
            excluded_symbols = open_symbols | closed_symbols_this_cycle
            actionable = [c for c in eligible if c.symbol not in excluded_symbols]

            if not actionable:
                action = {
                    "event": "blocked",
                    "reason": "eligible_symbols_already_open_or_just_closed",
                    "symbols": [c.symbol for c in eligible],
                }
            else:
                best = actionable[0]
                realized_pnl = self.state.realized_pnl_today()
                decision = check_risk(
                    settings=self.settings,
                    candidate=best,
                    realized_pnl_today=realized_pnl,
                    open_positions=len(open_positions),
                )

                if decision.allowed and self.settings.mode == "shadow":
                    is_new = self.state.claim_signal(
                        symbol=best.symbol,
                        signal_open_time=best.signal_open_time,
                        kind="shadow",
                    )
                    if is_new:
                        action = {"event": "shadow_signal", **best.to_dict()}
                        self.notifier.send(
                            f"V2 SHADOW SIGNAL {best.symbol}\n"
                            f"Setup: {best.entry_setup}\n"
                            f"Score: {best.score}/100\n"
                            f"Price: {best.price:.8f}\n"
                            f"RelVol: {best.relative_volume:.2f}x\n"
                            f"Taker buy: {best.taker_buy_ratio:.1%}"
                        )
                    else:
                        action = {
                            "event": "shadow_duplicate_suppressed",
                            "symbol": best.symbol,
                            "signal_open_time": best.signal_open_time,
                        }
                elif decision.allowed and self.settings.mode == "paper":
                    entry_price = books.get(best.symbol, {}).get("ask", best.price)
                    position = self.state.open_position(
                        symbol=best.symbol,
                        entry_price=entry_price,
                        quote_size=self.settings.trade_size_usdt,
                        take_profit_pct=self.settings.take_profit_pct,
                        stop_loss_pct=self.settings.stop_loss_pct,
                    )
                    action = {
                        "event": "paper_open",
                        "symbol": position.symbol,
                        "setup": best.entry_setup,
                        "score": best.score,
                        "entry_price": position.entry_price,
                        "quote_size": position.quote_size,
                        "take_profit": position.take_profit,
                        "stop_loss": position.stop_loss,
                    }
                    self.notifier.send(
                        f"V2 PAPER BUY {position.symbol}\n"
                        f"Setup: {best.entry_setup}\n"
                        f"Score: {best.score}/100\n"
                        f"Entry: {position.entry_price:.8f}\n"
                        f"TP: {position.take_profit:.8f}\n"
                        f"SL: {position.stop_loss:.8f}"
                    )
                elif decision.allowed and self.settings.mode == "live":
                    raise RuntimeError(
                        "LIVE_EXECUTION_LOCKED: protective Spot exit-order adapter is not implemented yet"
                    )
                else:
                    action = {
                        "event": "blocked",
                        "symbol": best.symbol,
                        "score": best.score,
                        "reason": decision.reason,
                    }

        summary = {
            "mode": self.settings.mode,
            "universe_size": len(universe),
            "evaluated": len(candidates),
            "eligible": len(eligible),
            "gate_failure_counts": gate_failure_counts,
            "top_near_miss": top_near_miss,
            "top": [c.to_dict() for c in candidates[:5]],
            "paper_events": paper_events,
            "action": action,
            "errors": errors,
            "realized_pnl_today": self.state.realized_pnl_today(),
            "open_positions": len(self.state.list_open_positions()),
        }
        return summary

    @staticmethod
    def dump_summary(summary: dict[str, Any]) -> str:
        return json.dumps(summary, ensure_ascii=False, sort_keys=True)
