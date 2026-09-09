from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN

PUBLIC_BASES = [
    'https://data-api.binance.vision/api/v3',
    'https://api.binance.com/api/v3',
]
_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_SEC = 900


def _get_json(path: str, params: dict) -> dict:
    q = urllib.parse.urlencode(params)
    last = None
    for base in PUBLIC_BASES:
        try:
            req = urllib.request.Request(
                f'{base}{path}?{q}',
                headers={'User-Agent': 'tst-binance-filter-normalizer/1.0', 'Accept': 'application/json'},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read() or b'{}')
        except Exception as exc:
            last = exc
    raise RuntimeError(f'EXCHANGE_INFO_UNAVAILABLE:{type(last).__name__ if last else "unknown"}')


def symbol_info(symbol: str) -> dict:
    symbol = str(symbol or '').upper()
    cached = _CACHE.get(symbol)
    if cached and time.time() - cached[0] < CACHE_SEC:
        return cached[1]
    row = _get_json('/exchangeInfo', {'symbol': symbol})
    symbols = row.get('symbols') or []
    if len(symbols) != 1:
        raise RuntimeError('SYMBOL_NOT_FOUND')
    info = symbols[0]
    if str(info.get('status') or '').upper() != 'TRADING':
        raise RuntimeError('SYMBOL_NOT_TRADING')
    if info.get('isSpotTradingAllowed') is False:
        raise RuntimeError('SPOT_NOT_ALLOWED')
    _CACHE[symbol] = (time.time(), info)
    return info


def _filters(info: dict) -> dict[str, dict]:
    return {str(x.get('filterType') or ''): x for x in (info.get('filters') or [])}


def _dec(value) -> Decimal:
    return Decimal(str(value))


def floor_step(value: float, step: str) -> float:
    d = _dec(value)
    s = _dec(step)
    if s <= 0:
        return float(d)
    units = (d / s).to_integral_value(rounding=ROUND_DOWN)
    return float(units * s)


def _min_notional(fs: dict[str, dict]) -> float:
    if 'NOTIONAL' in fs:
        try:
            return float(fs['NOTIONAL'].get('minNotional') or 0)
        except Exception:
            pass
    if 'MIN_NOTIONAL' in fs:
        try:
            return float(fs['MIN_NOTIONAL'].get('minNotional') or 0)
        except Exception:
            pass
    return 0.0


def normalize_buy_quote(symbol: str, quote_usdt: float) -> tuple[float, dict]:
    info = symbol_info(symbol)
    fs = _filters(info)
    min_notional = _min_notional(fs)
    quote = math.floor(float(quote_usdt) * 100.0) / 100.0
    if quote <= 0:
        raise RuntimeError('BAD_QUOTE_AMOUNT')
    if min_notional > 0 and quote + 1e-12 < min_notional:
        raise RuntimeError(f'BELOW_MIN_NOTIONAL:{min_notional:g}')
    return quote, {'minNotional': min_notional, 'status': info.get('status')}


def normalize_oco(symbol: str, quantity: float, tp: float, sl: float, sl_limit: float) -> tuple[dict, dict]:
    info = symbol_info(symbol)
    fs = _filters(info)
    pf = fs.get('PRICE_FILTER') or {}
    lot = fs.get('LOT_SIZE') or {}
    tick = str(pf.get('tickSize') or '0')
    step = str(lot.get('stepSize') or '0')
    min_qty = float(lot.get('minQty') or 0)
    max_qty = float(lot.get('maxQty') or 0)
    min_notional = _min_notional(fs)

    q = floor_step(float(quantity), step)
    ntp = floor_step(float(tp), tick)
    nsl = floor_step(float(sl), tick)
    nsl_limit = floor_step(float(sl_limit), tick)

    if q <= 0 or (min_qty > 0 and q + 1e-15 < min_qty):
        raise RuntimeError(f'BELOW_MIN_QTY:{min_qty:g}')
    if max_qty > 0 and q > max_qty + 1e-15:
        raise RuntimeError(f'ABOVE_MAX_QTY:{max_qty:g}')
    if not (nsl_limit > 0 and nsl > 0 and ntp > 0 and nsl_limit <= nsl < ntp):
        raise RuntimeError('BAD_OCO_LEVELS_AFTER_NORMALIZATION')
    if min_notional > 0:
        if q * nsl_limit + 1e-12 < min_notional:
            raise RuntimeError(f'STOP_LEG_BELOW_MIN_NOTIONAL:{min_notional:g}')
        if q * ntp + 1e-12 < min_notional:
            raise RuntimeError(f'TP_LEG_BELOW_MIN_NOTIONAL:{min_notional:g}')

    return {
        'quantity': q,
        'take_profit_price': ntp,
        'stop_loss_price': nsl,
        'stop_limit_price': nsl_limit,
    }, {
        'tickSize': tick,
        'stepSize': step,
        'minQty': min_qty,
        'maxQty': max_qty,
        'minNotional': min_notional,
    }
