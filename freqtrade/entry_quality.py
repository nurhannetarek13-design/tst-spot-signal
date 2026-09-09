from __future__ import annotations

import json
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BINANCE_BASES = [
    'https://data-api.binance.vision/api/v3',
    'https://api.binance.com/api/v3',
]

MIN_VOLUME_RATIO = float(os.getenv('FAST_MIN_VOLUME_RATIO', '0.95'))
MIN_TAKER_BUY_RATIO = float(os.getenv('FAST_MIN_TAKER_BUY_RATIO', '0.58'))
MIN_BREAKOUT_DISTANCE = float(os.getenv('FAST_MIN_BREAKOUT_DISTANCE', '0.0025'))
MAX_BREAKOUT_DISTANCE = float(os.getenv('FAST_MAX_BREAKOUT_DISTANCE', '0.0080'))
MAX_SPREAD_PCT = float(os.getenv('FAST_MAX_SPREAD_PCT', '0.12'))
MAX_MOVE_FROM_2H_LOW = float(os.getenv('FAST_MAX_MOVE_FROM_2H_LOW', '0.030'))
MAX_15M_IMPULSE = float(os.getenv('FAST_MAX_15M_IMPULSE', '0.018'))
BTC_MAX_15M_DROP = float(os.getenv('FAST_BTC_MAX_15M_DROP', '0.006'))
BTC_MAX_1H_DROP = float(os.getenv('FAST_BTC_MAX_1H_DROP', '0.012'))

# Strong first-touch momentum exception. This is deliberately stricter than
# the normal micro gate so we can catch ignition at resistance without
# turning the strategy into a chase/fake-breakout machine.
IGNITION_MIN_VOLUME_RATIO = float(os.getenv('FAST_IGNITION_MIN_VOLUME_RATIO', '1.35'))
IGNITION_MIN_TAKER_BUY_RATIO = float(os.getenv('FAST_IGNITION_MIN_TAKER_BUY_RATIO', '0.68'))
IGNITION_MAX_SPREAD_PCT = float(os.getenv('FAST_IGNITION_MAX_SPREAD_PCT', '0.08'))
IGNITION_MAX_WICK_RATIO = float(os.getenv('FAST_IGNITION_MAX_WICK_RATIO', '1.80'))
IGNITION_MIN_DISTANCE = float(os.getenv('FAST_IGNITION_MIN_DISTANCE', '-0.0010'))


def _get_json(url: str, timeout: int = 10):
    req = Request(url, headers={'User-Agent': 'tst-entry-quality/1.2', 'Accept': 'application/json'})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _api(path: str, params: dict):
    query = urlencode(params)
    last_error = None
    for base in BINANCE_BASES:
        try:
            return _get_json(f'{base}{path}?{query}')
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f'Binance quality API unavailable: {last_error}')


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    out = values[0]
    for value in values[1:]:
        out = alpha * value + (1.0 - alpha) * out
    return out


def _closed_klines(symbol: str, interval: str, limit: int) -> list:
    rows = _api('/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})
    now_ms = int(time.time() * 1000)
    rows = [row for row in rows if int(row[6]) < now_ms]
    if len(rows) < min(55, limit - 1):
        raise RuntimeError(f'insufficient {interval} candles for {symbol}')
    return rows


def _ignition_exception(m: dict) -> bool:
    distance = float(m.get('distance_to_breakout') or 0.0)
    return (
        IGNITION_MIN_DISTANCE <= distance < MIN_BREAKOUT_DISTANCE
        and float(m.get('volume_ratio') or 0.0) >= IGNITION_MIN_VOLUME_RATIO
        and float(m.get('taker_buy_ratio') or 0.0) >= IGNITION_MIN_TAKER_BUY_RATIO
        and bool(m.get('volume_accel'))
        and float(m.get('spread_pct') or 999.0) <= IGNITION_MAX_SPREAD_PCT
        and float(m.get('wick_ratio') or 999.0) <= IGNITION_MAX_WICK_RATIO
        and float(m.get('compression_ratio') or 999.0) <= 1.10
    )


def micro_gate(m: dict) -> tuple[bool, str]:
    ignition = _ignition_exception(m)
    if m['distance_to_breakout'] < MIN_BREAKOUT_DISTANCE and not ignition:
        return False, 'too-close-to-breakout'
    if m['distance_to_breakout'] > MAX_BREAKOUT_DISTANCE:
        return False, 'too-far-from-breakout'
    if m['volume_ratio'] < MIN_VOLUME_RATIO:
        return False, 'volume-too-weak'
    if m['taker_buy_ratio'] < MIN_TAKER_BUY_RATIO:
        return False, 'buy-pressure-too-weak'
    if m['spread_pct'] > MAX_SPREAD_PCT:
        return False, 'spread-too-wide'
    if m['compression_ratio'] > 1.10:
        return False, 'no-compression'
    if not m['volume_accel'] and m['volume_ratio'] < 1.20:
        return False, 'no-volume-confirmation'
    if m['wick_ratio'] > 2.5:
        return False, 'rejection-wick'
    return True, 'ignition-ok' if ignition else 'micro-ok'


def _trend_snapshot(symbol: str) -> dict:
    rows15 = _closed_klines(symbol, '15m', 80)
    rows1h = _closed_klines(symbol, '1h', 80)

    c15 = [float(row[4]) for row in rows15]
    l15 = [float(row[3]) for row in rows15]
    c1h = [float(row[4]) for row in rows1h]

    ema20_15 = _ema(c15[-60:], 20)
    ema50_15 = _ema(c15[-75:], 50)
    ema20_1h = _ema(c1h[-60:], 20)
    ema50_1h = _ema(c1h[-75:], 50)

    move_from_2h_low = c15[-1] / min(l15[-8:]) - 1.0
    recent_15m_returns = [c15[i] / c15[i - 1] - 1.0 for i in range(len(c15) - 4, len(c15))]
    max_recent_impulse = max(recent_15m_returns)

    trend15_ok = c15[-1] >= ema20_15 * 0.997 and ema20_15 >= ema50_15 * 0.995
    trend1h_ok = c1h[-1] >= ema20_1h * 0.992 and ema20_1h >= ema50_1h * 0.985

    return {
        'last15': c15[-1],
        'last1h': c1h[-1],
        'ema20_15': ema20_15,
        'ema50_15': ema50_15,
        'ema20_1h': ema20_1h,
        'ema50_1h': ema50_1h,
        'move_from_2h_low': move_from_2h_low,
        'max_recent_15m_impulse': max_recent_impulse,
        'trend15_ok': trend15_ok,
        'trend1h_ok': trend1h_ok,
    }


def _btc_regime() -> dict:
    rows15 = _closed_klines('BTCUSDT', '15m', 60)
    rows1h = _closed_klines('BTCUSDT', '1h', 60)
    c15 = [float(row[4]) for row in rows15]
    c1h = [float(row[4]) for row in rows1h]

    ema20_15 = _ema(c15[-50:], 20)
    ema20_1h = _ema(c1h[-50:], 20)
    mom15 = c15[-1] / c15[-2] - 1.0
    mom1h = c1h[-1] / c1h[-2] - 1.0

    ok = (
        mom15 >= -BTC_MAX_15M_DROP
        and mom1h >= -BTC_MAX_1H_DROP
        and c15[-1] >= ema20_15 * 0.994
        and c1h[-1] >= ema20_1h * 0.985
    )
    return {
        'ok': ok,
        'mom15': mom15,
        'mom1h': mom1h,
        'ema20_15': ema20_15,
        'ema20_1h': ema20_1h,
        'last15': c15[-1],
        'last1h': c1h[-1],
    }


def runtime_preflight() -> dict:
    """Prove the higher-timeframe/BTC data path is live before signals are allowed."""
    trend = _trend_snapshot('BTCUSDT')
    btc = _btc_regime()
    return {
        'ok': True,
        'btc_regime_ok': bool(btc['ok']),
        'trend15_ok': bool(trend['trend15_ok']),
        'trend1h_ok': bool(trend['trend1h_ok']),
        'move_from_2h_low': float(trend['move_from_2h_low']),
        'btc_mom15': float(btc['mom15']),
        'btc_mom1h': float(btc['mom1h']),
    }


def validate_entry(symbol: str, m: dict) -> tuple[bool, str, dict]:
    micro_ok, micro_reason = micro_gate(m)
    if not micro_ok:
        return False, micro_reason, {}

    trend = _trend_snapshot(symbol)
    if trend['move_from_2h_low'] > MAX_MOVE_FROM_2H_LOW:
        return False, '2h-move-already-spent', trend
    if trend['max_recent_15m_impulse'] > MAX_15M_IMPULSE:
        return False, '15m-impulse-already-spent', trend
    if not trend['trend15_ok']:
        return False, '15m-trend-not-confirmed', trend
    if not trend['trend1h_ok']:
        return False, '1h-trend-not-confirmed', trend

    btc = _btc_regime()
    context = {**trend, 'btc': btc, 'micro_reason': micro_reason}
    if not btc['ok']:
        return False, 'btc-regime-weak', context

    return True, 'quality-ignition-ok' if micro_reason == 'ignition-ok' else 'quality-ok', context
