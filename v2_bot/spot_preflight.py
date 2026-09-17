from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any


ZERO = Decimal("0")


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


@dataclass(frozen=True)
class SpotSymbolRules:
    symbol: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal | None

    @classmethod
    def from_exchange_info(cls, symbol_info: dict[str, Any]) -> "SpotSymbolRules":
        symbol = str(symbol_info.get("symbol", "")).strip()
        if not symbol:
            raise ValueError("exchangeInfo symbol is missing")
        if str(symbol_info.get("status", "")) != "TRADING":
            raise ValueError(f"{symbol} is not TRADING")
        if symbol_info.get("isSpotTradingAllowed") is False:
            raise ValueError(f"{symbol} does not allow Spot trading")

        filters = {
            str(item.get("filterType")): item
            for item in symbol_info.get("filters", [])
            if isinstance(item, dict)
        }
        price_filter = filters.get("PRICE_FILTER", {})
        lot_filter = filters.get("LOT_SIZE", {})
        min_notional_filter = filters.get("MIN_NOTIONAL", {})
        notional_filter = filters.get("NOTIONAL", {})

        tick_size = _d(price_filter.get("tickSize"))
        step_size = _d(lot_filter.get("stepSize"))
        if tick_size <= 0:
            raise ValueError(f"{symbol} PRICE_FILTER tickSize is invalid")
        if step_size <= 0:
            raise ValueError(f"{symbol} LOT_SIZE stepSize is invalid")

        min_notional_candidates = [
            _d(min_notional_filter.get("minNotional")),
            _d(notional_filter.get("minNotional")),
        ]
        min_notional = max(min_notional_candidates)
        max_notional_raw = _d(notional_filter.get("maxNotional"))
        max_notional = max_notional_raw if max_notional_raw > 0 else None

        return cls(
            symbol=symbol,
            tick_size=tick_size,
            min_price=_d(price_filter.get("minPrice")),
            max_price=_d(price_filter.get("maxPrice")),
            step_size=step_size,
            min_qty=_d(lot_filter.get("minQty")),
            max_qty=_d(lot_filter.get("maxQty")),
            min_notional=min_notional,
            max_notional=max_notional,
        )

    def normalize_price(self, price: Decimal) -> Decimal:
        return _floor_step(price, self.tick_size)

    def normalize_qty(self, quantity: Decimal) -> Decimal:
        return _floor_step(quantity, self.step_size)


@dataclass(frozen=True)
class ProtectedTradePreflight:
    allowed: bool
    reasons: tuple[str, ...]
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    take_profit_price: Decimal
    stop_loss_price: Decimal
    entry_notional: Decimal
    take_profit_notional: Decimal
    stop_loss_notional: Decimal
    min_notional: Decimal


def validate_protected_spot_trade(
    *,
    rules: SpotSymbolRules,
    quote_size: float | Decimal,
    entry_price: float | Decimal,
    take_profit_price: float | Decimal,
    stop_loss_price: float | Decimal,
) -> ProtectedTradePreflight:
    quote = _d(quote_size)
    entry = rules.normalize_price(_d(entry_price))
    take_profit = rules.normalize_price(_d(take_profit_price))
    stop_loss = rules.normalize_price(_d(stop_loss_price))
    reasons: list[str] = []

    if quote <= 0:
        reasons.append("quote_size_not_positive")
    if entry <= 0:
        reasons.append("entry_price_not_positive")
    if take_profit <= entry:
        reasons.append("take_profit_not_above_entry")
    if stop_loss <= 0 or stop_loss >= entry:
        reasons.append("stop_loss_not_below_entry")

    raw_qty = quote / entry if entry > 0 else ZERO
    quantity = rules.normalize_qty(raw_qty)
    if quantity <= 0:
        reasons.append("quantity_zero_after_step_rounding")
    if rules.min_qty > 0 and quantity < rules.min_qty:
        reasons.append("quantity_below_min_qty")
    if rules.max_qty > 0 and quantity > rules.max_qty:
        reasons.append("quantity_above_max_qty")

    for label, price in (
        ("entry", entry),
        ("take_profit", take_profit),
        ("stop_loss", stop_loss),
    ):
        if rules.min_price > 0 and price < rules.min_price:
            reasons.append(f"{label}_price_below_min_price")
        if rules.max_price > 0 and price > rules.max_price:
            reasons.append(f"{label}_price_above_max_price")

    entry_notional = quantity * entry
    take_profit_notional = quantity * take_profit
    stop_loss_notional = quantity * stop_loss

    if rules.min_notional > 0:
        if entry_notional < rules.min_notional:
            reasons.append("entry_notional_below_min")
        if take_profit_notional < rules.min_notional:
            reasons.append("take_profit_notional_below_min")
        if stop_loss_notional < rules.min_notional:
            reasons.append("stop_loss_notional_below_min")

    if rules.max_notional is not None:
        if entry_notional > rules.max_notional:
            reasons.append("entry_notional_above_max")
        if take_profit_notional > rules.max_notional:
            reasons.append("take_profit_notional_above_max")
        if stop_loss_notional > rules.max_notional:
            reasons.append("stop_loss_notional_above_max")

    return ProtectedTradePreflight(
        allowed=not reasons,
        reasons=tuple(reasons),
        symbol=rules.symbol,
        quantity=quantity,
        entry_price=entry,
        take_profit_price=take_profit,
        stop_loss_price=stop_loss,
        entry_notional=entry_notional,
        take_profit_notional=take_profit_notional,
        stop_loss_notional=stop_loss_notional,
        min_notional=rules.min_notional,
    )
