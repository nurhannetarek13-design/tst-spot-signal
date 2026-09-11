"""Canonical Binance USD-M microstructure features from ordered Tardis raw rows.

Research-only. No network access and no trading actions. This module reuses the
same snapshot/`pu` semantics proven by ``tardis_l2_replay`` and emits event-level
book features suitable for historical diagnostics.
"""
from __future__ import annotations

from decimal import Decimal
from statistics import median
from typing import Any, Iterable

from research.tardis_l2_replay import replay_ordered_rows

AUTHORIZATION = "RESEARCH_ONLY"
DEFAULT_LEVELS = (1, 5, 10)


def _side(rows: Iterable[Iterable[Any]]) -> dict[Decimal, Decimal]:
    return {
        Decimal(str(p)): Decimal(str(q))
        for p, q in rows
        if Decimal(str(q)) > 0
    }


def _update(book: dict[Decimal, Decimal], rows: Iterable[Iterable[Any]]) -> None:
    for p, q in rows:
        price = Decimal(str(p))
        qty = Decimal(str(q))
        if qty == 0:
            book.pop(price, None)
        else:
            book[price] = qty


def _top(book: dict[Decimal, Decimal], *, bid: bool, n: int):
    return sorted(book.items(), key=lambda x: x[0], reverse=bid)[:n]


def _timestamp_ms(event: dict[str, Any], prefix: str) -> int | None:
    for key in ("E", "T"):
        value = event.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    # Tardis raw prefixes are timestamps. Keep parsing deliberately tolerant;
    # event timestamps remain preferred whenever Binance supplies them.
    if prefix:
        try:
            from datetime import datetime
            text = prefix.rstrip("Z")
            dt = datetime.fromisoformat(text)
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    return None


def extract_microstructure_rows(
    rows: list[dict[str, Any]],
    *,
    symbol: str,
    levels: tuple[int, ...] = DEFAULT_LEVELS,
    date: str | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    """Validate canonical replay and emit an event-level microstructure table."""
    validation = replay_ordered_rows(rows, symbol=symbol, date=date, offset=offset)
    if not validation.get("canonicalReplayReady"):
        return {
            "status": "REPLAY_NOT_READY",
            "authorization": AUTHORIZATION,
            "liveTrading": False,
            "symbol": symbol,
            "validation": validation,
            "features": [],
        }

    snap_line = int(validation["snapshotLine"])
    snapshot = next(r for r in rows if int(r["line"]) == snap_line)["data"]
    bids = _side(snapshot["bids"])
    asks = _side(snapshot["asks"])
    prev_u = int(snapshot["lastUpdateId"])
    previous_depths: dict[int, tuple[Decimal, Decimal]] = {}
    features: list[dict[str, Any]] = []

    depth_rows = [
        r for r in rows
        if int(r["line"]) > snap_line and {"U", "u", "pu"}.issubset(r["data"])
    ]

    for r in depth_rows:
        event = r["data"]
        pu = int(event["pu"])
        if pu != prev_u:
            return {
                "status": "PU_GAP_DURING_FEATURE_EXTRACTION",
                "authorization": AUTHORIZATION,
                "liveTrading": False,
                "symbol": symbol,
                "validation": validation,
                "features": features,
                "gap": {"previousU": prev_u, "pu": pu, "u": int(event["u"])},
            }
        _update(bids, event.get("b", []))
        _update(asks, event.get("a", []))
        if not bids or not asks:
            return {
                "status": "EMPTY_BOOK",
                "authorization": AUTHORIZATION,
                "liveTrading": False,
                "symbol": symbol,
                "validation": validation,
                "features": features,
            }

        best_bid = max(bids)
        best_ask = min(asks)
        if best_bid >= best_ask:
            return {
                "status": "CROSSED_BOOK",
                "authorization": AUTHORIZATION,
                "liveTrading": False,
                "symbol": symbol,
                "validation": validation,
                "features": features,
                "u": int(event["u"]),
            }
        bid_qty = bids[best_bid]
        ask_qty = asks[best_ask]
        mid = (best_bid + best_ask) / Decimal("2")
        spread = best_ask - best_bid
        spread_bps = spread / mid * Decimal("10000")
        touch_total = bid_qty + ask_qty
        microprice = (
            (best_ask * bid_qty + best_bid * ask_qty) / touch_total
            if touch_total > 0 else mid
        )
        micro_dev_bps = (microprice / mid - Decimal("1")) * Decimal("10000")

        feat: dict[str, Any] = {
            "line": int(r["line"]),
            "timestamp_ms": _timestamp_ms(event, str(r.get("prefix") or "")),
            "update_id": int(event["u"]),
            "first_update_id": int(event["U"]),
            "previous_update_id": pu,
            "best_bid": float(best_bid),
            "best_bid_qty": float(bid_qty),
            "best_ask": float(best_ask),
            "best_ask_qty": float(ask_qty),
            "mid": float(mid),
            "spread_bps": float(spread_bps),
            "microprice": float(microprice),
            "microprice_deviation_bps": float(micro_dev_bps),
            "touch_queue_imbalance": float((bid_qty - ask_qty) / touch_total) if touch_total > 0 else 0.0,
        }

        for n in levels:
            bid_top = _top(bids, bid=True, n=n)
            ask_top = _top(asks, bid=False, n=n)
            bid_depth = sum((q for _, q in bid_top), Decimal("0"))
            ask_depth = sum((q for _, q in ask_top), Decimal("0"))
            total = bid_depth + ask_depth
            imbalance = (bid_depth - ask_depth) / total if total > 0 else Decimal("0")
            previous = previous_depths.get(n)
            ofi = None
            normalized = None
            if previous is not None:
                prev_bid, prev_ask = previous
                ofi_d = (bid_depth - prev_bid) - (ask_depth - prev_ask)
                ofi = float(ofi_d)
                normalized = float(ofi_d / total) if total > 0 else 0.0
            previous_depths[n] = (bid_depth, ask_depth)
            feat[f"bid_depth_{n}"] = float(bid_depth)
            feat[f"ask_depth_{n}"] = float(ask_depth)
            feat[f"depth_imbalance_{n}"] = float(imbalance)
            feat[f"ofi_depth_proxy_{n}"] = ofi
            feat[f"ofi_depth_proxy_norm_{n}"] = normalized

        features.append(feat)
        prev_u = int(event["u"])

    return {
        "status": "PASS",
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "symbol": symbol,
        "levels": list(levels),
        "validation": validation,
        "features": features,
    }


def sample_last_per_second(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the final feature observation in each Binance event-time second."""
    by_second: dict[int, dict[str, Any]] = {}
    for f in features:
        ts = f.get("timestamp_ms")
        if ts is None:
            continue
        by_second[int(ts) // 1000] = f
    return [by_second[k] for k in sorted(by_second)]


def _directional_outcomes(samples: list[dict[str, Any]], indices: list[int], side: str, horizons=(10, 30, 60)):
    result: dict[str, dict[str, Any]] = {}
    for h in horizons:
        vals: list[float] = []
        for i in indices:
            if i + h >= len(samples):
                continue
            p0 = float(samples[i]["mid"])
            p1 = float(samples[i + h]["mid"])
            r = (p1 / p0 - 1.0) * 10000.0
            vals.append(r if side == "long" else -r)
        result[str(h)] = {
            "n": len(vals),
            "meanDirectionalBps": (sum(vals) / len(vals)) if vals else None,
            "medianDirectionalBps": median(vals) if vals else None,
            "hitRate": (sum(v > 0 for v in vals) / len(vals)) if vals else None,
        }
    return result


def frozen_gate_g_diagnostic(samples: list[dict[str, Any]], cooldown_seconds: int = 60) -> dict[str, Any]:
    """Apply the already-frozen Gate G OBI+microprice definition."""
    outcomes: dict[str, Any] = {}
    event_counts: dict[str, int] = {}
    for side in ("long", "short"):
        candidates: list[int] = []
        for i, f in enumerate(samples):
            obi = float(f.get("depth_imbalance_10", 0.0))
            micro = float(f.get("microprice_deviation_bps", 0.0))
            ok = (obi >= 0.60 and micro >= 0.08) if side == "long" else (obi <= -0.60 and micro <= -0.08)
            if ok:
                candidates.append(i)
        kept: list[int] = []
        last_second: int | None = None
        for i in candidates:
            sec = int(samples[i]["timestamp_ms"]) // 1000
            if last_second is None or sec - last_second >= cooldown_seconds:
                kept.append(i)
                last_second = sec
        event_counts[side] = len(kept)
        outcomes[side] = _directional_outcomes(samples, kept, side)
    return {
        "eventDefinition": {
            "obi10AbsMin": 0.60,
            "micropriceDirectionalBpsMin": 0.08,
            "sampleSeconds": 1,
            "eventCooldownSeconds": cooldown_seconds,
        },
        "eventCounts": event_counts,
        "outcomes": outcomes,
    }


def summarize_features(features: list[dict[str, Any]], samples: list[dict[str, Any]]) -> dict[str, Any]:
    def stats(name: str):
        vals = [float(f[name]) for f in features if f.get(name) is not None]
        if not vals:
            return {"n": 0, "min": None, "median": None, "mean": None, "max": None}
        return {
            "n": len(vals),
            "min": min(vals),
            "median": median(vals),
            "mean": sum(vals) / len(vals),
            "max": max(vals),
        }
    return {
        "featureEvents": len(features),
        "sampledSeconds": len(samples),
        "spreadBps": stats("spread_bps"),
        "micropriceDeviationBps": stats("microprice_deviation_bps"),
        "depthImbalance10": stats("depth_imbalance_10"),
        "normalizedDepthPressure10": stats("ofi_depth_proxy_norm_10"),
    }
