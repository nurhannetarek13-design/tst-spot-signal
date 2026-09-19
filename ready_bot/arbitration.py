"""Shared-wallet PAPER arbitration: one fill per symbol, independent risk gates.

A high-priority signal rejected by the risk engine must not consume the symbol's
slot or suppress a DIFFERENT strategy's eligible signal on the same candle.
Neither indicators nor signal scores can override the wallet, order filters,
or the existing loss limits. No exchange order functions are present here.
"""
import math


def execute_candidates(signals, enabled, state, config, snapshots, open_fn):
    """Attempt frozen-priority signals, using the caller's actual risk/lot engine.

    Returns (rejections, accepted). `seen` is strategy-specific attempt dedupe;
    `seen_symbol` is updated ONLY following a successful fill. Never use a
    risk-rejected signal to block other strategies on that symbol.
    """
    priority = {strategy: n for n, strategy in enumerate(enabled)}
    if any(signal.get('strategy') not in priority for signal in signals):
        raise RuntimeError('UNREGISTERED_SIGNAL_IN_ARBITRATION')
    rejected, accepted = [], []
    for signal in sorted(signals, key=lambda s: (priority[s['strategy']], s['symbol'])):
        name, symbol, bar_time = signal['strategy'], signal['symbol'], signal['bar_time']
        if symbol in state['positions']:
            continue
        if state.get('seen', {}).get(f'{name}:{symbol}') == bar_time:
            continue
        if state.get('seen_symbol', {}).get(symbol) == bar_time:
            continue
        snapshot = snapshots.get(symbol)
        if not snapshot:
            rejected.append(dict(symbol=symbol, strategy=name, reason='MISSING_MARKET_SNAPSHOT'))
            continue
        stake = signal.get('max_stake_usdt', config['trade_size_usdt'])
        if not isinstance(stake, (int, float)) or not math.isfinite(stake) or stake <= 0:
            reason = 'INVALID_STRATEGY_STAKE'
        elif 'hold_bars' in signal and (type(signal['hold_bars']) is not int
                                        or not 1 <= signal['hold_bars'] <= 1000
                                        or signal.get('hold_interval') not in ('15m', '1h')):
            reason = 'INVALID_NATIVE_HOLD_CONTRACT'
        else:
            own_config = {**config, 'trade_size_usdt': min(config['trade_size_usdt'], stake)}
            # Simulated BUY at public best ask when available; a last trade is
            # not a guaranteed buy execution price. The core applies modeled
            # additional slippage and fee once, not twice.
            price = snapshot.get('ask', snapshot['price'])
            if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
                reason = 'INVALID_BUY_QUOTE'
            else:
                reason = open_fn(state, signal, price, own_config, snapshot['filters'])
        state.setdefault('seen', {})[f'{name}:{symbol}'] = bar_time
        if reason == 'PAPER_OPENED':
            position = state['positions'][symbol]
            if 'hold_bars' in signal:
                position['hold_bars'] = signal['hold_bars']
                position['hold_interval'] = signal['hold_interval']
            if 'native_source' in signal:
                position['native_source'] = signal['native_source']
            state.setdefault('seen_symbol', {})[symbol] = bar_time
            accepted.append(dict(symbol=symbol, strategy=name, bar_time=bar_time,
                                 reason='PAPER_OPENED'))
        else:
            rejected.append(dict(symbol=symbol, strategy=name, reason=reason))
    return rejected, accepted
