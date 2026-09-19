#!/usr/bin/env python3
"""One multi-strategy PAPER engine. This module contains no exchange order submission."""
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
    req = urllib.request.Request(url, headers={'User-Agent': 'tst-multi-paper/1.1'})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def market_request(route):
    last_error = None
    for base in ('https://data-api.binance.vision', 'https://api.binance.com'):
        try:
            return request_json(base + route)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f'Binance market data unavailable for {route}: {last_error}')


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
    if losses == 0:
        return 100 if gains else 50
    return 100 - 100 / (1 + gains / losses)


def candidate(strategy, symbol, price, stop, target, bar_time, reason):
    if not all(math.isfinite(x) for x in (price, stop, target)) or not 0 < stop < price < target:
        return None
    return dict(strategy=strategy, symbol=symbol, price=price, stop=stop,
                target=target, bar_time=bar_time, reason=reason)


def trend_breakout(symbol, htf, bars):
    if min(len(htf), len(bars)) < 220:
        return None
    higher, closes = [b['close'] for b in htf], [b['close'] for b in bars]
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
    higher, closes = [b['close'] for b in htf], [b['close'] for b in bars]
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
    higher, closes = [b['close'] for b in htf], [b['close'] for b in bars]
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


# Each entry function and its exits are independent. Register a new strategy only
# after supplying complete causal entry/exit rules and offline tests.
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


def loss_at_stop(position, config):
    """Modeled entry cost minus net stop proceeds, inclusive of entry/exit fees and exit slippage."""
    stop_fill = simulated_fill(position['stop'], 'sell', config)
    return max(0.0, position['cost'] - position['qty'] * stop_fill * (1 - config['fee_rate']))


def portfolio_stop_risk(state, config):
    return sum(loss_at_stop(pos, config) for pos in state['positions'].values())


def close_position(state, symbol, price, config, reason):
    position = state['positions'].pop(symbol)
    # A stop can gap against us; a target is conservatively modeled at its target.
    reference = min(price, position['stop']) if reason == 'STOP' else (
        position['target'] if reason == 'TARGET' else price)
    sell = simulated_fill(reference, 'sell', config)
    proceeds = position['qty'] * sell * (1 - config['fee_rate'])
    pnl = proceeds - position['cost']
    state['cash_usdt'] += proceeds
    state['day_pnl'] += pnl
    state['closed_trades'].append(dict(symbol=symbol, strategy=position['strategy'],
        entry=position['entry'], exit=sell, qty=position['qty'], pnl_usdt=pnl,
        reason=reason, opened_at=position['opened_at'], closed_at=utc_now()))
    return pnl


def open_position(state, signal, current_price, config, filters):
    if len(state['positions']) >= config['max_open_positions']:
        return 'MAX_OPEN_POSITIONS'
    symbol = signal['symbol']
    if symbol in state['positions']:
        return 'POSITION_ALREADY_OPEN'
    if state['day_pnl'] <= -config['max_daily_loss_usdt']:
        return 'DAILY_LOSS_LIMIT'
    if not (math.isfinite(current_price) and current_price > 0):
        return 'INVALID_QUOTE'
    if signal.get('strategy') not in STRATEGIES:
        return 'UNKNOWN_STRATEGY'
    entry = float(signal['price'])
    planned_stop = float(signal['stop'])
    planned_target = float(signal['target'])
    if not all(math.isfinite(v) for v in (entry, planned_stop, planned_target)) or not 0 < planned_stop < entry < planned_target:
        return 'INVALID_STRATEGY_LEVELS'
    planned_risk_fraction = (entry - planned_stop) / entry
    if not 0 < planned_risk_fraction <= config['max_stop_fraction']:
        return 'INVALID_STOP_DISTANCE'
    fee = config['fee_rate']
    notional = min(config['trade_size_usdt'], state['cash_usdt'] / (1 + fee))
    fill = simulated_fill(current_price, 'buy', config)
    if not math.isfinite(fill) or fill <= 0:
        return 'INVALID_QUOTE'
    if notional < filters['min_notional'] or notional > filters['max_notional']:
        return 'EXCHANGE_NOTIONAL_FILTER'
    qty = notional / fill
    step = filters['step_size']
    if not math.isfinite(step) or step <= 0:
        return 'INVALID_LOT_STEP'
    qty = math.floor(qty / step + 1e-10) * step
    if qty < filters['min_qty'] or qty > filters['max_qty']:
        return 'EXCHANGE_LOT_FILTER'
    if qty * fill < filters['min_notional'] or qty * fill > filters['max_notional']:
        return 'EXCHANGE_NOTIONAL_FILTER_AFTER_ROUNDING'
    cost = qty * fill * (1 + fee)
    if cost > state['cash_usdt'] + 1e-10:
        return 'INSUFFICIENT_CASH_AFTER_ROUNDING'
    # Preserve each strategy's independently defined stop AND target. The old
    # engine wrongly replaced RANGE_REVERSION's mean target with a universal 2R.
    position = dict(strategy=signal['strategy'], entry=fill, qty=qty, cost=cost,
        stop=fill * planned_stop / entry, target=fill * planned_target / entry,
        opened_at=utc_now(), bar_time=signal['bar_time'])
    actual_risk = loss_at_stop(position, config)
    if actual_risk > config['max_risk_usdt'] + 1e-10:
        return 'RISK_PER_TRADE'
    total_stop_risk = portfolio_stop_risk(state, config) + actual_risk
    if total_stop_risk > config.get('max_portfolio_risk_usdt', config['max_daily_loss_usdt']) + 1e-10:
        return 'PORTFOLIO_STOP_RISK'
    if max(0.0, -state['day_pnl']) + total_stop_risk > config['max_daily_loss_usdt'] + 1e-10:
        return 'REMAINING_DAILY_RISK'
    state['cash_usdt'] -= cost
    state['positions'][symbol] = position
    return 'PAPER_OPENED'


def symbol_filters(symbol):
    data = market_request(f'/api/v3/exchangeInfo?symbol={symbol}')
    entry = next((s for s in data.get('symbols', []) if s.get('symbol') == symbol and
                  s.get('status') == 'TRADING' and s.get('isSpotTradingAllowed', True)), None)
    if not entry:
        raise RuntimeError(f'Non-trading Spot market: {symbol}')
    rows = {f['filterType']: f for f in entry.get('filters', [])}
    lots = rows.get('LOT_SIZE')
    notional = rows.get('NOTIONAL') or rows.get('MIN_NOTIONAL')
    if not lots or not notional:
        raise RuntimeError(f'Missing exchange filters: {symbol}')
    return dict(min_notional=float(notional['minNotional']),
                max_notional=float(notional.get('maxNotional', 'Infinity')),
                min_qty=float(lots['minQty']), max_qty=float(lots['maxQty']),
                step_size=float(lots['stepSize']))


def pick_signals(signals, enabled, state):
    """Deterministic priority; never use several correlated signals as 'votes'."""
    order = {name: i for i, name in enumerate(enabled)}
    selected = {}
    for signal in sorted(signals, key=lambda s: (order[s['strategy']], s['symbol'])):
        symbol = signal['symbol']
        if symbol not in selected and symbol not in state['positions']:
            key = f"{signal['strategy']}:{symbol}"
            if (state.get('seen', {}).get(key) != signal['bar_time']
                    and state.get('seen_symbol', {}).get(symbol) != signal['bar_time']):
                selected[symbol] = signal
    return list(selected.values())


def validate_config(config):
    if config.get('mode') != 'paper':
        raise RuntimeError('This bot only supports PAPER mode')
    enabled = config['strategies']
    if not enabled or len(set(enabled)) != len(enabled) or any(x not in STRATEGIES for x in enabled):
        raise RuntimeError('Invalid strategy registry')
    if not config.get('symbols') or any(not s.endswith('USDT') for s in config['symbols']):
        raise RuntimeError('Invalid Spot USDT symbol universe')
    for key in ('starting_cash_usdt', 'trade_size_usdt', 'max_daily_loss_usdt',
                'max_risk_usdt', 'max_stop_fraction', 'max_open_positions'):
        if not float(config[key]) > 0:
            raise RuntimeError(f'Invalid positive risk setting: {key}')
    for key in ('fee_rate', 'slippage_rate'):
        if not 0 <= float(config[key]) < 1:
            raise RuntimeError(f'Invalid trading cost: {key}')
    if not 0 < float(config.get('max_portfolio_risk_usdt', config['max_daily_loss_usdt'])) <= config['max_daily_loss_usdt']:
        raise RuntimeError('Invalid portfolio risk cap')


def run(config):
    validate_config(config)
    enabled = config['strategies']
    state = read_state(config)
    today = datetime.now(timezone.utc).date().isoformat()
    if state['day'] != today:
        state['day'], state['day_pnl'] = today, 0.0
    market, blocked = {}, []
    for symbol in config['symbols']:
        try:
            bars = closed_bars(symbol, config['execution_interval'], 250)
            htf = closed_bars(symbol, config['trend_interval'], 250)
            quote = market_request(f'/api/v3/ticker/price?symbol={symbol}')
            price = float(quote['price'])
            filters = symbol_filters(symbol)
            if len(bars) < 220 or len(htf) < 220 or not (math.isfinite(price) and price > 0):
                raise RuntimeError('Insufficient closed bars or invalid quote')
            market[symbol] = htf, bars, price, filters
        except Exception as exc:
            blocked.append(dict(symbol=symbol, reason='DATA_OR_FILTER_UNAVAILABLE', detail=str(exc)[:140]))
    unpriced_position = False
    for symbol in list(state['positions']):
        if symbol not in market:
            blocked.append(dict(symbol=symbol, reason='OPEN_POSITION_PRICE_UNAVAILABLE'))
            unpriced_position = True
            continue
        price, position = market[symbol][2], state['positions'][symbol]
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
    if unpriced_position:
        blocked.append(dict(reason='GLOBAL_ENTRY_HALT_OPEN_POSITION_UNPRICED'))
    else:
        for signal in pick_signals(signals, enabled, state):
            symbol = signal['symbol']
            reason = open_position(state, signal, market[symbol][2], config, market[symbol][3])
            state['seen'][f"{signal['strategy']}:{symbol}"] = signal['bar_time']
            state.setdefault('seen_symbol', {})[symbol] = signal['bar_time']
            if reason != 'PAPER_OPENED':
                blocked.append(dict(symbol=symbol, strategy=signal['strategy'], reason=reason))
    state['signals'] = [{k: s[k] for k in ('symbol', 'strategy', 'bar_time', 'reason')} for s in signals]
    state['blocked'], state['last_run'] = blocked, utc_now()
    save_state(state)
    print(json.dumps(dict(mode='PAPER_ONLY', cash_usdt=state['cash_usdt'],
        day_pnl=state['day_pnl'], positions=state['positions'],
        signals=state['signals'], blocked=blocked, last_run=state['last_run']), indent=2))
    return state


if __name__ == '__main__':
    run(json.loads(CONFIG_PATH.read_text()))
