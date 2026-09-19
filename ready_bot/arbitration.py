"""Shared-wallet PAPER arbitration: one fill per symbol, independent risk gates.

A rejected high-priority signal cannot consume the symbol lock and suppress a
different independently eligible strategy. Every accepted trade still passes
the existing wallet, fee, lot, stop and aggregate risk checks. No live orders.
"""
import math


LOCK_SCHEMA_VERSION = 2


def reconcile_legacy_symbol_locks(state):
    """One-time safe migration from the old 'reject == symbol lock' behavior.

    Only clear stale symbol locks when there is affirmative evidence that the
    account has NEVER opened a paper position: no current/closed trades and an
    explicit empty accepted-signals record. Retain every strategy-specific
    attempt marker, so an individually rejected strategy is NOT retried on
    that same candle. If evidence is absent or ambiguous, preserve old locks.
    This does not relax any stop, notional, daily loss or portfolio risk gate.
    """
    if state.get('symbol_lock_schema_version') == LOCK_SCHEMA_VERSION:
        return 0
    removed = 0
    if (state.get('accepted_signals') == [] and state.get('positions') == {}
            and state.get('closed_trades') == []):
        removed = len(state.get('seen_symbol', {}))
        state['seen_symbol'] = {}
    state['symbol_lock_schema_version'] = LOCK_SCHEMA_VERSION
    return removed


def execute_candidates(signals, enabled, state, config, snapshots, open_fn):
    """Attempt frozen-priority signals using the same actual risk/lot engine.

    Returns (rejections, accepted). `seen` deduplicates per-strategy attempts;
    `seen_symbol` is recorded only after a successful paper fill. A rejected
    signal cannot stop a DIFFERENT strategy for the same symbol and candle.
    """
    reconcile_legacy_symbol_locks(state)
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
            # Best public ask when available; core applies additional modeled
            # slippage and fees once. Never pretend last trade is a guaranteed
            # buy execution price.
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
