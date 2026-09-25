"""Event-driven microstructure replay validator.

Purpose:
- validate V3-like execution assumptions on timestamped depth/trade events
- enforce no-lookahead at decision time
- include spread, depth-aware slippage, fees, latency and partial-fill logic
- carry explicit survivorship-bias metadata

Input: JSONL events sorted or unsorted by ts_ms. Supported types:
  depth_snapshot, depth_update, agg_trade, signal, mark

This is research/validation only and cannot place orders.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from ready_bot.production_guard import estimate_buy_slippage


@dataclass
class Book:
    bids: dict[float, float]
    asks: dict[float, float]
    last_update_id: int = 0
    last_ts_ms: int = 0
    sequence_gaps: int = 0

    @classmethod
    def empty(cls):
        return cls({}, {})

    def snapshot(self, bids, asks, update_id, ts_ms):
        self.bids = {float(p): float(q) for p, q in bids if float(q) > 0}
        self.asks = {float(p): float(q) for p, q in asks if float(q) > 0}
        self.last_update_id = int(update_id or 0)
        self.last_ts_ms = int(ts_ms)

    def update(self, bids, asks, first_id, final_id, ts_ms):
        if self.last_update_id and int(final_id) <= self.last_update_id:
            return "OLD"
        if self.last_update_id and int(first_id) > self.last_update_id + 1:
            self.sequence_gaps += 1
            return "GAP"
        for side, rows in ((self.bids, bids), (self.asks, asks)):
            for p0, q0 in rows or []:
                p, q = float(p0), float(q0)
                if q <= 0:
                    side.pop(p, None)
                else:
                    side[p] = q
        self.last_update_id = int(final_id)
        self.last_ts_ms = int(ts_ms)
        return "APPLY"

    def best_bid(self):
        return max(self.bids) if self.bids else None

    def best_ask(self):
        return min(self.asks) if self.asks else None

    def as_depth(self, levels=100):
        asks = [[str(p), str(self.asks[p])] for p in sorted(self.asks)[:levels]]
        bids = [[str(p), str(self.bids[p])] for p in sorted(self.bids, reverse=True)[:levels]]
        return {"asks": asks, "bids": bids}


def _sorted_events(events):
    return sorted(events, key=lambda e: (int(e.get("ts_ms") or 0), str(e.get("type") or "")))


def _profit_factor(pnls):
    gross_win = sum(x for x in pnls if x > 0)
    gross_loss = abs(sum(x for x in pnls if x < 0))
    if gross_loss == 0:
        return float("inf") if gross_win > 0 else 0.0
    return gross_win / gross_loss


def _max_drawdown(equity_curve):
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for x in equity_curve:
        peak = max(peak, x)
        if peak > 0:
            worst = max(worst, (peak - x) / peak)
    return worst


def _future_mark(marks, symbol, after_ts_ms, horizon_ms):
    rows = marks.get(symbol) or []
    target = int(after_ts_ms) + int(horizon_ms)
    chosen = None
    for ts, price in rows:
        if ts < target:
            continue
        chosen = (ts, price)
        break
    return chosen


def replay(events, *,
           initial_equity=1000.0,
           quote_per_trade=10.0,
           fee_rate=0.001,
           latency_ms=500,
           max_slippage_bps=12.0,
           min_fill_ratio=0.999,
           max_book_age_ms=3000,
           horizon_ms=15 * 60_000,
           universe_source="UNKNOWN",
           survivorship_bias_checked=False):
    events = _sorted_events(events)
    books = defaultdict(Book.empty)
    trades = defaultdict(lambda: deque(maxlen=10000))
    marks = defaultdict(list)
    decisions = []
    pnls = []
    equity = float(initial_equity)
    curve = [equity]
    lookahead_violations = 0
    stale_rejections = 0
    gap_rejections = 0
    partial_fill_rejections = 0
    slippage_rejections = 0
    symbols_traded = set()

    # Marks are outcome data only; never exposed to decision features.
    for e in events:
        if e.get("type") == "mark":
            marks[str(e["symbol"])].append((int(e["ts_ms"]), float(e["price"])))
    for rows in marks.values():
        rows.sort()

    for e in events:
        et = str(e.get("type") or "")
        symbol = str(e.get("symbol") or "")
        ts = int(e.get("ts_ms") or 0)
        if et == "depth_snapshot":
            books[symbol].snapshot(e.get("bids") or [], e.get("asks") or [], e.get("last_update_id") or 0, ts)
            continue
        if et == "depth_update":
            status = books[symbol].update(
                e.get("bids") or [], e.get("asks") or [],
                e.get("first_update_id") or 0, e.get("final_update_id") or 0, ts
            )
            if status == "GAP":
                gap_rejections += 1
            continue
        if et == "agg_trade":
            quote = float(e["price"]) * float(e["qty"])
            delta = -quote if bool(e.get("buyer_maker")) else quote
            trades[symbol].append((ts, delta))
            continue
        if et != "signal":
            continue

        decision_ts = ts
        exec_ts = decision_ts + int(latency_ms)
        # No event after exec_ts may influence execution features.
        book = books[symbol]
        if book.last_ts_ms > exec_ts:
            lookahead_violations += 1
            decisions.append({"symbol": symbol, "ts_ms": ts, "code": "LOOKAHEAD_VIOLATION"})
            continue
        if exec_ts - book.last_ts_ms > int(max_book_age_ms):
            stale_rejections += 1
            decisions.append({"symbol": symbol, "ts_ms": ts, "code": "STALE_BOOK"})
            continue
        if book.sequence_gaps > 0 and e.get("require_gap_free", True):
            gap_rejections += 1
            decisions.append({"symbol": symbol, "ts_ms": ts, "code": "DEPTH_SEQUENCE_GAP"})
            continue

        bid, ask = book.best_bid(), book.best_ask()
        if not bid or not ask or ask < bid:
            decisions.append({"symbol": symbol, "ts_ms": ts, "code": "INVALID_BOOK"})
            continue

        quality = estimate_buy_slippage(book.as_depth(), quote_per_trade)
        if quality["fill_ratio"] < min_fill_ratio:
            partial_fill_rejections += 1
            decisions.append({"symbol": symbol, "ts_ms": ts, "code": "PARTIAL_FILL_REJECT", **quality})
            continue
        if quality["slippage_bps"] is None or quality["slippage_bps"] > max_slippage_bps:
            slippage_rejections += 1
            decisions.append({"symbol": symbol, "ts_ms": ts, "code": "SLIPPAGE_REJECT", **quality})
            continue

        fill = float(quality["average_price"])
        outcome = _future_mark(marks, symbol, exec_ts, horizon_ms)
        if not outcome:
            decisions.append({"symbol": symbol, "ts_ms": ts, "code": "NO_OUTCOME_MARK"})
            continue
        out_ts, out_price = outcome
        gross = (float(out_price) / fill) - 1.0
        net = gross - 2.0 * float(fee_rate)
        pnl = float(quote_per_trade) * net
        equity += pnl
        curve.append(equity)
        pnls.append(pnl)
        symbols_traded.add(symbol)
        decisions.append({
            "symbol": symbol,
            "ts_ms": ts,
            "exec_ts_ms": exec_ts,
            "exit_ts_ms": out_ts,
            "code": "FILLED",
            "fill_price": fill,
            "exit_price": out_price,
            "slippage_bps": quality["slippage_bps"],
            "fill_ratio": quality["fill_ratio"],
            "gross_return": gross,
            "net_return_after_fees": net,
            "pnl": pnl,
        })

    wins = sum(x > 0 for x in pnls)
    losses = sum(x < 0 for x in pnls)
    return {
        "mode": "RESEARCH_REPLAY_ONLY",
        "universe_source": universe_source,
        "survivorship_bias_checked": bool(survivorship_bias_checked),
        "lookahead_violations": lookahead_violations,
        "trades": len(pnls),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(pnls) if pnls else 0.0,
        "net_pnl": sum(pnls),
        "net_expectancy": sum(pnls) / len(pnls) if pnls else 0.0,
        "profit_factor": _profit_factor(pnls),
        "max_drawdown": _max_drawdown(curve),
        "symbols_traded": sorted(symbols_traded),
        "symbol_count": len(symbols_traded),
        "stale_rejections": stale_rejections,
        "gap_rejections": gap_rejections,
        "partial_fill_rejections": partial_fill_rejections,
        "slippage_rejections": slippage_rejections,
        "assumptions": {
            "fee_rate_per_side": fee_rate,
            "latency_ms": latency_ms,
            "max_slippage_bps": max_slippage_bps,
            "min_fill_ratio": min_fill_ratio,
            "max_book_age_ms": max_book_age_ms,
            "horizon_ms": horizon_ms,
        },
        "decisions": decisions,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--universe-source", default="UNKNOWN")
    ap.add_argument("--survivorship-bias-checked", action="store_true")
    ap.add_argument("--latency-ms", type=int, default=500)
    args = ap.parse_args()

    events = [json.loads(line) for line in Path(args.events).read_text().splitlines() if line.strip()]
    result = replay(
        events,
        latency_ms=args.latency_ms,
        universe_source=args.universe_source,
        survivorship_bias_checked=args.survivorship_bias_checked,
    )
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True))
    print(json.dumps({k: v for k, v in result.items() if k != "decisions"}, indent=2))


if __name__ == "__main__":
    main()
