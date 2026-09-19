#!/usr/bin/env python3
"""Native-source multi-strategy PAPER router. No private API access or order functions.

The JS bridge executes the original JS entry logic. Freqtrade classes with unavailable
native runtime/dependencies or incomplete exit semantics are reported as BLOCKED,
not silently rewritten into counterfeit strategies.
"""
import json
import math
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import multi_bot as core

BASE = Path(__file__).resolve().parents[1]
BRIDGE = Path(__file__).with_name('native_js_bridge.mjs')
REGIME = 'REGIME_MOMENTUM_PAPER_2R_WRAPPER'
SMALL_CAP = 'SMALL_CAP_INTRADAY_MOMENTUM_V1'
NATIVE_SOURCE_IDS = {
    REGIME: 'REGIME_ADAPTIVE_RISK_MANAGED_MOMENTUM_V1',
    SMALL_CAP: SMALL_CAP,
}
FREQTRADE_FILES = {
    'TST_ALLIGATOR_SMC_V2': 'AdaptiveRegimeStrategy.py',
    'BASTION_JOAT_SPOT_V1': 'BastionJOATSpotV1.py',
    'BASTION_JOAT_SPOT_V2': 'BastionJOATSpotV2.py',
    'NFI_PROTECTED_X7': 'NFIProtectedX7.py',
    'UNIFIED_CANDIDATE': 'UnifiedCandidateStrategy.py',
}
FREQTRADE_BLOCKS = {
    'TST_ALLIGATOR_SMC_V2': 'Native Freqtrade/TA-Lib runtime, 15m structure and full exit parity not installed in this 1h paper engine',
    'BASTION_JOAT_SPOT_V1': 'Original source explicitly lacks validated exit parity; no autonomous paper trade',
    'BASTION_JOAT_SPOT_V2': 'Depends on V1/Freqtrade and custom trailing, 2R and NY-session exits; native parity absent',
    'NFI_PROTECTED_X7': 'Parent NostalgiaForInfinityX7 is absent from repository; class always vetoes automatic entry',
    'UNIFIED_CANDIDATE': 'Manifest is validation/forward only; fingerprint/OOS unresolved and L2 conditions cannot be proxied by candles',
}


def _native_only(*_args):
    raise RuntimeError('Native source must execute through native_js_bridge.mjs')


def register_native():
    core.STRATEGIES[REGIME] = _native_only
    core.STRATEGIES[SMALL_CAP] = _native_only


def source_registry():
    entries = [dict(id=name, status='PAPER_UNPROVEN', source='ready_bot/multi_bot.py')
               for name in ('TREND_BREAKOUT', 'TREND_PULLBACK', 'RANGE_REVERSION')]
    entries.extend([
        dict(id=REGIME, status='PAPER_UNPROVEN_WRAPPED_EXIT', source='src/strategies/regime-adaptive-momentum.mjs',
             note='Native entry; NEW paper-only fixed 2R target and 24 closed 1h bar time exit; not source-exit parity'),
        dict(id=SMALL_CAP, status='PAPER_UNPROVEN', source='src/strategies/small-cap-intraday.mjs',
             note='Native entry, native ATR stop/target and 8 closed 15m bar time exit; min-notional may block a 5.50 USDT stake'),
    ])
    for name, filename in FREQTRADE_FILES.items():
        path = BASE / 'freqtrade' / 'user_data' / 'strategies' / filename
        entries.append(dict(id=name, status='BLOCKED_DEPENDENCY_OR_PARITY' if path.is_file() else 'BLOCKED_MISSING_SOURCE',
                            source=str(path.relative_to(BASE)), note=FREQTRADE_BLOCKS[name]))
    return entries


def rich_closed_bars(symbol, timeframe, limit=250):
    rows = core.market_request(f'/api/v3/klines?symbol={symbol}&interval={timeframe}&limit={limit}')
    now_ms = time.time() * 1000
    return [dict(open_time=int(r[0]), open=float(r[1]), high=float(r[2]),
                 low=float(r[3]), close=float(r[4]), volume=float(r[5]),
                 quote_volume=float(r[7]), taker_buy_quote=float(r[10]))
            for r in rows if int(r[6]) < now_ms]


def native_score(source, payload):
    """Fail closed on missing Node, malformed output, timeout or missing original module."""
    call = subprocess.run(['node', str(BRIDGE)], input=json.dumps(dict(source=source, input=payload)),
                          text=True, capture_output=True, timeout=15, check=False, cwd=BASE)
    if call.returncode != 0:
        raise RuntimeError('NATIVE_SCORER_FAILED:' + call.stderr.strip()[:100])
    result = json.loads(call.stdout)
    if not isinstance(result, dict) or result.get('strategy') != (
            'REGIME_ADAPTIVE_RISK_MANAGED_MOMENTUM_V1' if source == 'momentum' else SMALL_CAP):
        raise RuntimeError('INVALID_NATIVE_STRATEGY_ID')
    if result.get('liveApproved') is not False:
        raise RuntimeError('NATIVE_LIVE_APPROVAL_CHANGED')
    return result


def _rows(rows):
    return [dict(o=x['open'], h=x['high'], l=x['low'], c=x['close'], v=x['volume']) for x in rows]


def _flow(row):
    if row['quote_volume'] <= 0:
        raise RuntimeError('MISSING_TAKER_FLOW')
    return row['taker_buy_quote'] / row['quote_volume']


def _rvol(rows):
    if len(rows) < 21:
        raise RuntimeError('INSUFFICIENT_RVOL_HISTORY')
    prior = sum(x['volume'] for x in rows[-21:-1]) / 20
    if prior <= 0:
        raise RuntimeError('INVALID_RVOL_BASELINE')
    return rows[-1]['volume'] / prior


def native_signal(name, symbol, snapshot, btc):
    if name == REGIME:
        candles, benchmark = snapshot.get('1h_rich'), btc.get('1h_rich')
        if not candles or not benchmark:
            raise RuntimeError('MOMENTUM_NEEDS_1H_BTC_AND_ASSET')
        inputs = dict(symbol=symbol, candles=_rows(candles), btcCandles=_rows(benchmark),
                      quoteVolume24h=snapshot['quote_vol'], bid=snapshot['bid'], ask=snapshot['ask'],
                      takerBuyRatio=_flow(candles[-1]), relativeVolume=_rvol(candles))
        result = native_score('momentum', inputs)
        if not result.get('ok'):
            return None
        entry, stop = float(result['entry']), float(result['stop'])
        # The JS source has NO target or exit. This adapter is explicitly a NEW,
        # unvalidated PAPER variant; never pass it off as the original exit.
        target = entry + 2 * (entry - stop)
        signal = core.candidate(name, symbol, entry, stop, target,
                                candles[-1]['open_time'], 'Native JS momentum; paper adapter 2R')
        if signal:
            signal.update(max_stake_usdt=float(result['notional']), hold_bars=24, hold_interval='1h',
                          native_source=NATIVE_SOURCE_IDS[name])
        return signal
    if name == SMALL_CAP:
        candles, hourly, btc15 = snapshot.get('15m'), snapshot.get('1h_rich'), btc.get('15m')
        if not candles or not hourly or not btc15:
            raise RuntimeError('SMALL_CAP_NEEDS_15M_1H_BTC')
        inputs = dict(symbol=symbol, baseAsset=symbol.removesuffix('USDT'),
                      c15=_rows(candles), c1h=_rows(hourly), btc15=_rows(btc15),
                      quoteVolume24h=snapshot['quote_vol'], bid=snapshot['bid'], ask=snapshot['ask'],
                      takerBuyRatio=_flow(candles[-1]), relativeVolume=_rvol(candles))
        result = native_score('small_cap', inputs)
        if not result.get('ok'):
            return None
        signal = core.candidate(name, symbol, float(result['entry']), float(result['stop']),
                                float(result['target']), candles[-1]['open_time'], 'Original JS small-cap scorer')
        if signal:
            signal.update(max_stake_usdt=float(result['notional']), hold_bars=int(result['maxHoldBars']),
                          hold_interval='15m', native_source=NATIVE_SOURCE_IDS[name])
        return signal
    raise RuntimeError('UNKNOWN_NATIVE_SOURCE')


def validate(config):
    register_native()
    core.validate_config(config)
    if any(name in FREQTRADE_FILES for name in config['strategies']):
        raise RuntimeError('Freqtrade sources cannot be enabled without native parity and dependencies')
    if not BRIDGE.is_file():
        raise RuntimeError('Native JS bridge missing')


def run(config):
    validate(config)
    state = core.read_state(config)
    today = datetime.now(timezone.utc).date().isoformat()
    if state['day'] != today:
        state['day'], state['day_pnl'] = today, 0.0
    state['source_registry'] = source_registry()
    enabled = config['strategies']
    snapshots, blocked = {}, []
    for symbol in config['symbols']:
        try:
            bars = core.closed_bars(symbol, config['execution_interval'], 250)
            htf = core.closed_bars(symbol, config['trend_interval'], 250)
            price = float(core.market_request(f'/api/v3/ticker/price?symbol={symbol}')['price'])
            filters = core.symbol_filters(symbol)
            if min(len(bars), len(htf)) < 220 or not (math.isfinite(price) and price > 0):
                raise RuntimeError('INSUFFICIENT_CLOSED_BARS_OR_QUOTE')
            snapshots[symbol] = dict(bars=bars, htf=htf, price=price, filters=filters)
        except Exception as exc:
            blocked.append(dict(symbol=symbol, reason='DATA_OR_FILTER_UNAVAILABLE', detail=str(exc)[:140]))
    # A native-module market-data failure only disables its signal; it must
    # never disable price monitoring for unrelated existing paper positions.
    if any(s in enabled for s in NATIVE_SOURCE_IDS):
        for symbol, snap in snapshots.items():
            try:
                snap['1h_rich'] = rich_closed_bars(symbol, '1h')
                snap['15m'] = rich_closed_bars(symbol, '15m')
                book = core.market_request(f'/api/v3/ticker/bookTicker?symbol={symbol}')
                tick = core.market_request(f'/api/v3/ticker/24hr?symbol={symbol}')
                snap.update(bid=float(book['bidPrice']), ask=float(book['askPrice']),
                            quote_vol=float(tick['quoteVolume']))
                if not (snap['bid'] > 0 and snap['ask'] > snap['bid'] and snap['quote_vol'] > 0):
                    raise RuntimeError('BAD_ORDER_BOOK_OR_LIQUIDITY')
            except Exception as exc:
                blocked.append(dict(symbol=symbol, reason='NATIVE_DATA_UNAVAILABLE', detail=str(exc)[:140]))
        benchmark = snapshots.get('BTCUSDT', {})
    else:
        benchmark = {}
    unpriced = False
    for symbol in list(state['positions']):
        snap = snapshots.get(symbol)
        if not snap:
            blocked.append(dict(symbol=symbol, reason='OPEN_POSITION_UNPRICED'))
            unpriced = True
            continue
        pos, price = state['positions'][symbol], snap['price']
        if price <= pos['stop']:
            core.close_position(state, symbol, price, config, 'STOP')
        elif price >= pos['target']:
            core.close_position(state, symbol, price, config, 'TARGET')
        elif pos['strategy'] in NATIVE_SOURCE_IDS:
            interval = '15m' if pos['strategy'] == SMALL_CAP else '1h'
            bars = snap.get(interval)
            if not bars:
                blocked.append(dict(symbol=symbol, reason='NATIVE_TIME_EXIT_DATA_UNAVAILABLE'))
                unpriced = True
            elif bars[-1]['open_time'] - pos['bar_time'] >= (8 * 15 if interval == '15m' else 24 * 60) * 60_000:
                core.close_position(state, symbol, price, config, 'TIME_EXIT')
    signals = []
    for symbol, snap in snapshots.items():
        for name in enabled:
            try:
                sig = (native_signal(name, symbol, snap, benchmark) if name in NATIVE_SOURCE_IDS else
                       core.STRATEGIES[name](symbol, snap['htf'], snap['bars']))
                if sig:
                    signals.append(sig)
            except Exception as exc:
                blocked.append(dict(symbol=symbol, strategy=name, reason='SIGNAL_SOURCE_FAILED',
                                    detail=str(exc)[:140]))
    if unpriced:
        blocked.append(dict(reason='GLOBAL_ENTRY_HALT_UNPRICED_POSITION'))
    else:
        for signal in core.pick_signals(signals, enabled, state):
            symbol = signal['symbol']
            stake = signal.get('max_stake_usdt', config['trade_size_usdt'])
            if not (math.isfinite(stake) and stake > 0):
                reason = 'NATIVE_INVALID_STAKE'
            else:
                own_config = {**config, 'trade_size_usdt': min(config['trade_size_usdt'], stake)}
                reason = core.open_position(state, signal, snapshots[symbol]['price'], own_config,
                                            snapshots[symbol]['filters'])
            state['seen'][f"{signal['strategy']}:{symbol}"] = signal['bar_time']
            state.setdefault('seen_symbol', {})[symbol] = signal['bar_time']
            if reason != 'PAPER_OPENED':
                blocked.append(dict(symbol=symbol, strategy=signal['strategy'], reason=reason))
    state['signals'] = [{k: s[k] for k in ('symbol', 'strategy', 'bar_time', 'reason')} for s in signals]
    state['blocked'], state['last_run'] = blocked, core.utc_now()
    core.save_state(state)
    print(json.dumps(dict(mode='PAPER_ONLY', cash_usdt=state['cash_usdt'], day_pnl=state['day_pnl'],
                          positions=state['positions'], signals=state['signals'],
                          blocked=blocked, source_registry=state['source_registry'],
                          last_run=state['last_run']), indent=2))
    return state


if __name__ == '__main__':
    run(json.loads(core.CONFIG_PATH.read_text()))
