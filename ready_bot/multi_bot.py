#!/usr/bin/env python3
"""One multi-strategy PAPER trading engine. Does not submit exchange orders."""
import json
import math
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv('TST_MULTI_CONFIG', str(ROOT / 'multi_config.json')))
STATE_PATH = Path(os.getenv('TST_MULTI_STATE', str(ROOT / 'multi_state.json')))


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def request_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'tst-multi-paper/1.0'})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def market_request(route):
    error = None
    for base in ('https://data-api.binance.vision', 'https://api.binance.com'):
        try:
            return request_json(base + route)
        except Exception as exc:
            error = exc
    raise RuntimeError(f'Binance market data unavailable for {route}: {error}')


def closed_bars(symbol, timeframe='1h', limit=250):
    rows = market_request(f'/api/v3/klines?symbol={symbol}&interval={timeframe}&limit={limit}')
    now = time.time() * 1000
    return [dict(open_time=int(r[0]), open=float(r[1]), high=float(r[2]),
                 low=float(r[3]), close=float(r[4]), volume=float(r[5]))
            for r in rows if int(r[6]) < now]


def ema(values, n):
    if len(values) < n:
        return None
    value = sum(values[:n]) / n
    alpha = 2 / (n + 1)
    for item in values[n:]:
        value = item * alpha + value * (1 - alpha)
    return value


def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    ranges = [max(b['high'] - b['low'], abs(b['high'] - bars[i-1]['close']),
                  abs(b['low'] - bars[i-1]['close']))
              for i, b in enumerate(bars) if i > 0]
    return sum(ranges[-n:]) / n


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    changes = [closes[i] - closes[i-1] for i in range(len(closes)-n, len(closes))]
    gains = sum(max(v, 0) for v in changes) / n
    losses = sum(max(-v, 0) for v in changes) / n
    return 100 if losses == 0 else 100 - 100 / (1 + gains / losses)


def candidate(strategy, symbol, price, stop, target, bar_time, reason):
    if not all(math.isfinite(x) for x in (price, stop, target)) or not 0 < stop < price < target:
        return None
    return dict(strategy=strategy, symbol=symbol, price=price, stop=stop,
                target=target, bar_time=bar_time, reason=reason)


def trend_breakout(symbol, htf, bars):
    if min(len(htf), len(bars)) < 220:
        return None
    higher = [b['close'] for b in htf]
    closes = [b['close'] for b in bars]
    if not (ema(higher, 50) > ema(higher, 200) and ema(closes, 50) > ema(closes, 200)):
        return None
    last = bars[-1]
    avg_volume = sum(b['volume'] for b in bars[-21:-1]) / 20
    if not (last['close'] > max(b['high'] for b in bars[-21:-1])
            and last['volume'] >= 1.2 * avg_volume):
        return None
    risk = 1.5 * atr(bars)
    return candidate('TREND_BREAKOUT', symbol, last['close'], last['close'] - risk,
                     last['close'] + 2 * risk, last['open_time'], '20-bar high breakout')


def trend_pullback(symbol, htf, bars):
    if min(len(htf), len(bars)) < 220:
        return None
    higher = [b['close'] for b in htf]
    closes = [b['close'] for b in bars]
    if not (ema(higher, 50) > ema(higher, 200) and ema(closes, 50) > ema(closes, 200)):
        return None
    prev_ema20, ema20 = ema(closes[:-1], 20), ema(closes, 20)
    if not (closes[-2] <= prev_ema20 and closes[-1] > ema20 and closes[-1] > bars[-2]['high']):
        return None
    risk = 1.5 * atr(bars)
    return candidate('TREND_PULLBACK', symbol, closes[-1], closes[-1] - risk,
                     closes[-1] + 2 * risk, bars[-1]['open_time'], 'EMA20 recovery')


def range_reversion(symbol, htf, bars):
    if min(len(htf), len(bars)) < 220:
        return None
    higher = [b['close'] for b in htf]
    closes = [b['close'] for b in bars]
    if abs(ema(higher, 50) / ema(higher, 200) - 1) > 0.01:
        return None
    window = closes[-21:-1]
    mean = sum(window) / len(window)
    std = math.sqrt(sum((v - mean)**2 for v in window) / len(window))
    last = bars[-1]
    if not (std > 0 and last['low'] < mean - 2 * std
            and last['close'] > mean - 2 * std and last['close'] > last['open']
            and rsi(closes) < 40):
        return None
    risk = 1.5 * atr(bars)
    return candidate('RANGE_REVERSION', symbol, last['close'], last['close'] - risk,
                     mean, last['open_time'], 'Lower-band reclaim, range regime')


STRATEGIES = {
    'TREND_BREAKOUT': trend_breakout,
    'TREND_PULLBACK': trend_pullback,
    'RANGE_REVERSION': range_reversion,
}


def initial_state(config):
    return dict(mode='PAPER_ONLY', cash_usdt=config['starting_cash_usdt'],
                positions={}, closed_trades=[], last_run=None,
                day=datetime.now(timezone.utc).date().isoformat(), day_pnl=0.0,
                seen={}, seen_symbol={}, blocked=[], signals=[])


def read_state(config):
    if not STATE_PATH.exists():
        return initial_state(config)
    state = json.loads(STATE_PATH.read_text())
    if state.get('mode') != 'PAPER_ONLY':
        raise RuntimeError('Refusing non-paper state')
    return state


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    staged = STATE_PATH.with_suffix('.tmp')
    staged.write_text(json.dumps(state, indent=2, sort_keys=True))
    staged.replace(STATE_PATH)


def simulated_fill(price, side, config):
    slip = config['slippage_rate']
    return price * (1 + slip if side == 'buy' else 1 - slip)


def close_position(state, symbol, price, config, reason):
    position = state['positions'].pop(symbol)
    sell = simulated_fill(price, 'sell', config)
    proceeds = position['qty'] * sell * (1 - config['fee_rate'])
    pnl = proceeds - position['cost']
    state['cash_usdt'] += proceeds
    state['day_pnl'] += pnl
    state['closed_trades'].append(dict(symbol=symbol, strategy=position['strategy'],
                                       pnl_usdt=pnl, reason=reason, closed_at=utc_now()))
    return pnl


def open_position(state, signal, current_price, config, filters):
    if len(state['positions']) >= config['max_open_positions']:
        return 'MAX_OPEN_POSITIONS'
    symbol = signal['symbol']
    if symbol in state['positions']:
        return 'POSITION_ALREADY_OPEN'
    if state['day_pnl'] <= -config['max_daily_loss_usdt']:
        return 'DAILY_LOSS_LIMIT'
    fee = config['fee_rate']
    notional = min(config['trade_size_usdt'], state['cash_usdt'] / (1 + fee))
    fill = simulated_fill(current_price, 'buy', config)
    # Stops/targets are anchored at the simulated fill, not the stale signal close.
    planned_risk_fraction = (signal['price'] - signal['stop']) / signal['price']
    if not 0 < planned_risk_fraction <= config['max_stop_fraction']:
        return 'INVALID_STOP_DISTANCE'
    risk = fill * planned_risk_fraction
    actual_risk = notional * planned_risk_fraction + notional * 2 * (fee + config['slippage_rate'])
    if actual_risk > config['max_risk_usdt']:
        return 'RISK_PER_TRADE'
    if max(0.0, -state['day_pnl']) + actual_risk > config['max_daily_loss_usdt']:
        return 'REMAINING_DAILY_RISK'
    if notional < filters['min_notional'] or notional > filters['max_notional']:
        return 'EXCHANGE_NOTIONAL_FILTER'
    if notional * (1 + fee) > state['cash_usdt']:
        return 'INSUFFICIENT_CASH'
    qty = notional / fill
    step = filters['step_size']
    if step > 0:
        qty = math.floor((qty + 1e-12) / step) * step
    if qty < filters['min_qty'] or qty > filters['max_qty']:
        return 'EXCHANGE_LOT_FILTER'
    if qty * fill < filters['min_notional']:
        return 'EXCHANGE_NOTIONAL_FILTER_AFTER_ROUNDING'
    cost = qty * fill * (1 + fee)
    if cost > state['cash_usdt']:
        return 'INSUFFICIENT_CASH_AFTER_ROUNDING'
    state['cash_usdt'] -= cost
    state['positions'][symbol] = dict(strategy=signal['strategy'], entry=fill,
        qty=qty, cost=cost, stop=fill-risk, target=fill+2*risk,
        opened_at=utc_now(), bar_time=signal['bar_time'])
    return 'PAPER_OPENED'


def symbol_filters(symbol):
    data = market_request(f'/api/v3/exchangeInfo?symbol={symbol}')
    entry = next((s for s in data.get('symbols', []) if s.get('symbol') == symbol and
                  s.get('status') == 'TRADING' and s.get('isSpotTradingAllowed', True)), None)
    if not entry:
        raise RuntimeError(f'Non-trading Spot market: {symbol}')
    row = {f['filterType']: f for f in entry.get('filters', [])}
    lots = row.get('LOT_SIZE')
    notional = row.get('NOTIONAL') or row.get('MIN_NOTIONAL')
    if not lots or not notional:
        raise RuntimeError(f'Missing exchange filters: {symbol}')
    return dict(min_notional=float(notional['minNotional']),
                max_notional=float(notional.get('maxNotional', 'Infinity')),
                min_qty=float(lots['minQty']), max_qty=float(lots['maxQty']),
                step_size=float(lots['stepSize']))


def pick_signals(signals, enabled, state):
    """Deterministic one-position-per-symbol arbitration, never aggregate signals as votes."""
    order = {name: i for i, name in enumerate(enabled)}
    selected = {}
    for signal in sorted(signals, key=lambda s: (order[s['strategy']], s['symbol'])):
        if signal['symbol'] not in selected and signal['symbol'] not in state['positions']:
            key = f"{signal['strategy']}:{signal['symbol']}"
            already_used_symbol = state.get('seen_symbol', {}).get(signal['symbol']) == signal['bar_time']
            if state['seen'].get(key) != signal['bar_time'] and not already_used_symbol:
                selected[signal['symbol']] = signal
    return list(selected.values())


def run(config):
    if config.get('mode') != 'paper':
        raise RuntimeError('This bot only supports PAPER mode')
    enabled = config['strategies']
    if not enabled or len(set(enabled)) != len(enabled) or any(x not in STRATEGIES for x in enabled):
        raise RuntimeError('Invalid strategy registry')
    if any(float(config[x]) < 0 for x in ('fee_rate', 'slippage_rate')):
        raise RuntimeError('Negative trading costs')
    state = read_state(config)
    today = datetime.now(timezone.utc).date().isoformat()
    if state['day'] != today:
        state['day'], state['day_pnl'] = today, 0.0
    market = {}
    blocked = []
    # One unavailable symbol must not silently substitute synthetic prices.
    for symbol in config['symbols']:
        try:
            bars = closed_bars(symbol, config['execution_interval'], 250)
            htf = closed_bars(symbol, config['trend_interval'], 250)
            ticker = market_request(f'/api/v3/ticker/price?symbol={symbol}')
            price = float(ticker['price'])
            filters = symbol_filters(symbol)
            if len(bars) < 220 or len(htf) < 220 or not price > 0:
                raise RuntimeError('Insufficient closed bars or invalid quote')
            market[symbol] = htf, bars, price, filters
        except Exception as exc:
            blocked.append(dict(symbol=symbol, reason='DATA_OR_FILTER_UNAVAILABLE', detail=str(exc)[:140]))
    # A failed quote cannot close an existing position: keep it recorded and block new entries.
    for symbol in list(state['positions']):
        if symbol not in market:
            blocked.append(dict(symbol=symbol, reason='OPEN_POSITION_PRICE_UNAVAILABLE'))
            continue
        price = market[symbol][2]
        position = state['positions'][symbol]
        if price <= position['stop']:
            close_position(state, symbol, price, config, 'STOP')
        elif price >= position['target']:
            close_position(state, symbol, price, config, 'TARGET')
    signals = []
    for symbol, (htf, bars, _, _) in market.items():
        for name in enabled:
            signal = STRATEGIES[name](symbol, htf, bars)
            if signal:
                signals.append(signal)
    for signal in pick_signals(signals, enabled, state):
        symbol = signal['symbol']
        reason = open_position(state, signal, market[symbol][2], config, market[symbol][3])
        # Consume bar after decision, to prevent duplicate orders on the same candle.
        state['seen'][f"{signal['strategy']}:{symbol}"] = signal['bar_time']
        state.setdefault('seen_symbol', {})[symbol] = signal['bar_time']
        if reason != 'PAPER_OPENED':
            blocked.append(dict(symbol=symbol, strategy=signal['strategy'], reason=reason))
    state['signals'] = [{k: s[k] for k in ('symbol', 'strategy', 'bar_time', 'reason')} for s in signals]
    state['blocked'] = blocked
    state['last_run'] = utc_now()
    save_state(state)
    print(json.dumps(dict(mode='PAPER_ONLY', cash_usdt=state['cash_usdt'],
        day_pnl=state['day_pnl'], positions=state['positions'],
        signals=state['signals'], blocked=state['blocked'], last_run=state['last_run']), indent=2))
    return state


if __name__ == '__main__':
    run(json.loads(CONFIG_PATH.read_text()))
